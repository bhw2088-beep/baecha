"""
매칭 AI(LightGBM 기사 수락 예측) 성능 지표 — 발표용 수치 산출 전용 스크립트          약 10~30초

VS Code: 이 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 바로 실행되고, 끝나면 터미널에 지표 표가 나온다.
  - 이 PC 에 lightgbm · numpy 가 없으면 도커 이미지(baecha-app:dev)에서 자동으로 다시 실행 → Docker Desktop 필요
  - 실행이 성공하면 결과 표가 이 파일 맨 아래 ''' ''' 블록에 자동으로 저장된다 (문자열이라 실행되지 않음)
터미널: 수정본 폴더에서  PYTHONPATH=. python tests/eval_matching_lgbm.py

기존 코드 보호
  - backend/ 코드와 test_*.py 는 수정하지 않는다. backend/engines/matcher/ml_ranker.py 의 함수를 불러다 쓰기만 한다.
  - 서비스가 쓰는 모델 파일(backend/data/ml_ranker.txt)은 덮어쓰지 않는다 (train(save=False) 로 메모리에서만 학습).
  - 파일 이름이 test_ 로 시작하지 않고 tests/test_suites.py 목록에도 없어서 pytest · CI 가 자동 실행하지 않는다.

평가 방법 (서비스 학습과 같은 조건)
  - 데이터: ml_ranker.generate_synthetic() 가상 제안 8,000건 (시드 2026) — 기사 수락 행동 시뮬레이션
  - 분리: 앞 80% 학습 (6,400건) · 뒤 20% 검증 (X_val · y_val, 1,600건) — 검증셋은 학습에 쓰지 않음
  - 지표: ROC-AUC · PR-AUC(Average Precision) · LogLoss · 임계값 0.75 기준 Precision / Recall / F1
  - 비교 기준: 휴리스틱 모의 스코어러(mock_lgb_score · LightGBM 을 못 쓸 때 대체) · 전부 수락 예측
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "baecha-app:dev"
THRESHOLD = 0.75                              # ml_ranker 자동 제안 임계값 (보수적)
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9]       # 임계값별 비교
FEATURE_KO = {                                # 피처 중요도 표시용 한글 설명 (ml_ranker.FEATURES)
    "pickup_km": "상차지까지 거리", "util": "적재율", "fare_gap": "적정 운임 대비 제시 운임",
    "trip_km": "운행 거리", "cold": "콜드체인 여부", "urgent": "긴급 여부", "accept_rate": "기사 과거 수락률",
    "cf_score": "권역 익숙도", "rating": "기사 평점", "rush_hour": "러시아워 여부", "weather": "날씨",
    "ds_ratio": "권역 수요÷공급",
}
RESULT_MARK = "# ===== 마지막 실행 결과 (▷ 실행이 끝나면 자동으로 바뀜 · " + "문자열이라 코드로 실행되지 않음) ====="


# ------------------------------------------------------------------ 지표 (scikit-learn 없이 직접 계산)
def roc_auc(y: list[int], p: list[float]) -> float:
    """수락 건을 거절 건보다 높게 점수 매길 확률 (동점은 평균 순위)."""
    order = sorted(range(len(p)), key=lambda i: p[i])
    ranks = [0.0] * len(p)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return 0.5
    return (sum(r for r, t in zip(ranks, y) if t) - pos * (pos + 1) / 2) / (pos * neg)


def pr_auc(y: list[int], p: list[float]) -> float:
    """Average Precision: 점수 높은 순으로 볼 때, 수락 건을 만날 때마다의 정밀도 평균."""
    pairs = sorted(zip(p, y), key=lambda t: -t[0])
    pos = sum(y)
    if not pos:
        return 0.0
    tp = 0
    ap = 0.0
    for n, (_, t) in enumerate(pairs, 1):
        if t:
            tp += 1
            ap += tp / n
    return ap / pos


def log_loss(y: list[int], p: list[float]) -> float:
    eps = 1e-15
    return -sum(t * math.log(min(max(q, eps), 1 - eps)) + (1 - t) * math.log(min(max(1 - q, eps), 1 - eps))
                for t, q in zip(y, p)) / len(y)


def at_threshold(y: list[int], p: list[float], th: float) -> dict:
    tp = sum(1 for t, q in zip(y, p) if q >= th and t)
    fp = sum(1 for t, q in zip(y, p) if q >= th and not t)
    fn = sum(1 for t, q in zip(y, p) if q < th and t)
    tn = len(y) - tp - fp - fn
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"th": th, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
            "offered": (tp + fp) / len(y)}


# ------------------------------------------------------------------ 평가
def evaluate() -> str:
    from backend.engines.matcher import ml_ranker as mr

    t0 = time.perf_counter()
    X, y = mr.generate_synthetic()                    # 서비스 학습과 같은 데이터 · 시드
    cut = int(len(X) * 0.8)                           # 서비스 학습과 같은 분리
    X_val, y_val = X[cut:], y[cut:]
    r = mr.train(save=False)                          # 메모리에서만 학습 (모델 파일 덮어쓰지 않음)
    import numpy as np
    p_lgb = [float(v) for v in r["model"].predict(np.array(X_val))]
    p_mock = [mr.mock_lgb_score(dict(zip(mr.FEATURES, row))) for row in X_val]
    base_rate = sum(y[:cut]) / cut
    p_const = [base_rate] * len(y_val)               # 전부 '학습셋 평균 수락률'로 예측 (하한선)
    sec = time.perf_counter() - t0

    def row(name, p):
        m = at_threshold(y_val, p, THRESHOLD)
        return (f"| {name} | **{roc_auc(y_val, p):.3f}** | {pr_auc(y_val, p):.3f} | {log_loss(y_val, p):.3f} | "
                f"{m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.3f} |")

    main = at_threshold(y_val, p_lgb, THRESHOLD)
    pos_val = sum(y_val)
    imp = sorted(r["meta"]["importance"].items(), key=lambda kv: -kv[1])
    total_imp = sum(v for _, v in imp) or 1
    md = [
        "## 매칭 AI (LightGBM 기사 수락 예측) 성능 지표",
        "",
        f"- 데이터: 가상 제안 {len(X):,}건 (시드 {mr.SEED}) · 학습 {cut:,}건 · 검증 {len(X_val):,}건 "
        f"(검증 수락 {pos_val:,}건 · 수락률 {pos_val / len(y_val):.1%})",
        f"- 모델: LightGBM binary · 피처 {len(mr.FEATURES)}개 · 단조 제약 · 자동 제안 임계값 {THRESHOLD}",
        "",
        f"| 모델 | ROC-AUC | PR-AUC | LogLoss | Precision@{THRESHOLD} | Recall@{THRESHOLD} | F1@{THRESHOLD} |",
        "|---|---:|---:|---:|---:|---:|---:|",
        row("LightGBM (서비스 모델)", p_lgb),
        row("휴리스틱 모의 스코어러 (대체용)", p_mock),
        row("평균 수락률 일괄 예측 (하한선)", p_const),
        "",
        f"### 임계값 {THRESHOLD} 혼동 행렬 (LightGBM · 검증 {len(y_val):,}건)",
        "",
        "| | 실제 수락 | 실제 거절 |",
        "|---|---:|---:|",
        f"| 제안함 (점수 ≥ {THRESHOLD}) | {main['tp']:,} (TP) | {main['fp']:,} (FP) |",
        f"| 제안 안 함 | {main['fn']:,} (FN) | {main['tn']:,} (TN) |",
        "",
        f"- 제안 대상 비율 {main['offered']:.1%} · 제안한 기사 중 실제 수락 {main['precision']:.1%} "
        f"→ 거절률 약 {1 - main['precision']:.1%}",
        "",
        "### 임계값별 비교 (LightGBM)",
        "",
        "| 임계값 | 제안 비율 | Precision | Recall | F1 |",
        "|---:|---:|---:|---:|---:|",
        *[(lambda m: f"| {m['th']} | {m['offered']:.1%} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.3f} |")(
            at_threshold(y_val, p_lgb, th)) for th in SWEEP],
        "",
        "### 피처 중요도 (gain 비중 상위 5)",
        "",
        *[f"- {k} ({FEATURE_KO.get(k, k)}): {v / total_imp:.1%}" for k, v in imp[:5]],
        "",
        f"- 실행 {sec:.1f}초 · 가상(시뮬레이션) 데이터 기준 — 실운행 수락 기록으로 재학습하면 수치가 달라질 수 있음",
        "",
        *glossary(main, pos_val / len(y_val)),
        "",
        *explain({
            "lgb": {"auc": roc_auc(y_val, p_lgb), "ll": log_loss(y_val, p_lgb)},
            "mock": {"auc": roc_auc(y_val, p_mock), **at_threshold(y_val, p_mock, THRESHOLD)},
            "const": {"auc": roc_auc(y_val, p_const), "ll": log_loss(y_val, p_const),
                      **at_threshold(y_val, p_const, THRESHOLD)},
            "main": main, "sweep": [at_threshold(y_val, p_lgb, th) for th in SWEEP],
            "imp": [(k, v / total_imp) for k, v in imp[:2]],
        }),
    ]
    return "\n".join(md)


def explain(c: dict) -> list[str]:
    """결과 해설 (이번 실행 수치로 자동 작성) — 용어 정리와 함께 결과 블록에만 저장, 터미널에는 안 나옴."""
    lgb, mock, const, m = c["lgb"], c["mock"], c["const"], c["main"]
    offered = m["tp"] + m["fp"]
    actual_pos = m["tp"] + m["fn"]
    actual_neg = m["fp"] + m["tn"]
    best = max(c["sweep"], key=lambda s: s["f1"])
    lines = [
        "### 결과 해설",
        "",
        "**1. 모델 비교표**",
        "",
        f"- LightGBM(서비스 모델)이 대부분 지표에서 가장 좋다. 하한선(모델 없이 모두 같은 점수) 대비 "
        f"ROC-AUC {const['auc']:.3f} → **{lgb['auc']:.3f}** (수락 · 거절 기사를 구분하는 힘), "
        f"LogLoss {const['ll']:.3f} → **{lgb['ll']:.3f}** (예측 확률이 실제에 더 가까움).",
        f"- 휴리스틱 대체 스코어러는 ROC-AUC {mock['auc']:.3f} 로 순서 매기기는 비슷하지만, {THRESHOLD} 기준으로는 "
        f"너무 깐깐해 Recall {mock['recall']:.1%} — 받았을 기사의 상당수를 놓친다 (LightGBM 을 못 쓸 때만 대체용).",
        f"- 하한선의 F1({const['f1']:.3f})이 높아 보이는 것은 '전부 제안'이라 Recall 이 {const['recall']:.0%} 가 되기 "
        "때문이지 실력이 아니다 → 모델 실력은 ROC-AUC · LogLoss 로 비교한다.",
        "",
        f"**2. 혼동 행렬 (임계값 {THRESHOLD})**",
        "",
        f"- 제안한 기사 {offered:,}명 중 {m['tp']:,}명이 수락 → Precision {m['precision']:.1%} "
        f"(거절 {m['fp']:,}건 · 약 {1 - m['precision']:.1%}).",
        f"- 실제로 수락할 기사 {actual_pos:,}명 중 {m['tp']:,}명에게 제안 → Recall {m['recall']:.1%} "
        f"(놓친 기사 {m['fn']:,}명).",
        f"- 거절할 기사 {actual_neg:,}명 중 {m['tn']:,}명({m['tn'] / max(actual_neg, 1):.0%})에게는 "
        "미리 제안하지 않아 헛제안을 막았다.",
        "",
        "**3. 임계값별 비교 — 왜 0.75 인가**",
        "",
        "| 임계값 | 제안 비율 | 거절률 (100% − Precision) | 놓친 기사 (100% − Recall) |",
        "|---:|---:|---:|---:|",
        *[f"| {s['th']} | {s['offered']:.1%} | {1 - s['precision']:.1%} | {1 - s['recall']:.1%} |"
          for s in c["sweep"]],
        "",
        "- 임계값을 올리면 확실한 기사에게만 제안 → 거절은 줄지만 놓치는 기사가 늘고, 내리면 그 반대다.",
        f"- F1 만 보면 {best['th']}({best['f1']:.3f})가 가장 높지만, 거절은 기사 · 화주 시간 낭비와 재배차 지연으로 "
        "비용이 크고, 놓친 기사는 다음 후보에게 제안이 넘어가면 되므로 손해가 작다 → 거절을 줄이는 보수적 기준을 쓴다.",
        f"- {THRESHOLD} 에서 거절률 {1 - m['precision']:.1%} · 놓친 기사 {1 - m['recall']:.1%} — "
        "이보다 올리면 거절은 조금 줄지만 놓치는 기사가 빠르게 늘어난다. KPI 배차 거절률 목표(15~20% 미만)도 만족.",
        "",
        "**4. 피처 중요도**",
        "",
    ]
    (k1, v1), (k2, v2) = c["imp"]
    lines += [
        f"- 수락 여부를 가장 크게 좌우하는 것은 {FEATURE_KO.get(k1, k1)}({v1:.1%}), 다음이 "
        f"{FEATURE_KO.get(k2, k2)}({v2:.1%}) → 가까운 공차 기사에게 적정 운임으로 먼저 제안하는 매칭 방식의 근거.",
        "- 단, 가상 기사의 행동 규칙을 만들 때 상차 거리를 가장 크게 반영했으므로 모델이 그 규칙을 배운 결과다. "
        "실제 운행 기록으로 재학습하면 순위가 달라질 수 있다.",
        "",
        "**발표용 한 줄**",
        "",
        f"- \"AI 가 제안한 기사 10명 중 약 {round(m['precision'] * 10)}명이 수락했고, 수락할 기사의 "
        f"{m['recall']:.0%}를 찾아냈습니다. 모델 없이 찍는 것 대비 ROC-AUC 가 {const['auc']:.2f} → {lgb['auc']:.2f} "
        "로 올라갑니다. (가상 데이터 기준)\"",
    ]
    return lines


GLOSSARY_HEAD = "### 용어 정리"


def for_terminal(text: str) -> str:
    """터미널에는 수치만 (용어 정리는 파일 아래 결과 블록에만 저장)."""
    return text.split("\n" + GLOSSARY_HEAD)[0].rstrip() + "\n\n(용어 정리 · 결과 해설은 이 파일 맨 아래 결과 블록에 저장됨)"


def glossary(m: dict, val_rate: float) -> list[str]:
    """결과 표 아래 용어 정리 (이번 실행 수치로 예시) — 결과 블록에만 저장, 터미널에는 안 나옴."""
    return [
        GLOSSARY_HEAD,
        "",
        "| 용어 | 뜻 | 좋은 방향 |",
        "|---|---|:---:|",
        "| 수락 확률 (점수) | 모델이 예측한 '이 기사가 이 화물 제안을 수락할 확률' 0~1 | - |",
        f"| 임계값 ({THRESHOLD}) | 점수가 이 값 이상인 기사에게만 자동 제안 (높일수록 신중하게 · 적게 제안) | - |",
        "| 학습셋 / 검증셋 | 모델을 가르친 데이터 / 학습에 쓰지 않고 성능을 잰 데이터 (처음 보는 문제로 시험) | - |",
        "| TP (참 양성) | 제안했고 실제로 수락 — 맞힌 제안 | 많을수록 좋음 |",
        "| FP (거짓 양성) | 제안했는데 거절 — 헛제안 (기사 · 화주 시간 낭비) | 적을수록 좋음 |",
        "| FN (거짓 음성) | 제안 안 했는데 실제로는 수락했을 기사 — 놓친 기사 | 적을수록 좋음 |",
        "| TN (참 음성) | 제안 안 했고 실제로도 거절 — 잘 걸러낸 경우 | 많을수록 좋음 |",
        "| 혼동 행렬 | 위 TP · FP · FN · TN 4칸을 표로 정리한 것 | - |",
        f"| Precision (정밀도) | 제안한 기사 중 실제 수락 비율 = TP ÷ (TP + FP) · 이번 {m['precision']:.1%} | 높을수록 좋음 |",
        f"| Recall (재현율) | 실제 수락할 기사 중 모델이 찾아낸 비율 = TP ÷ (TP + FN) · 이번 {m['recall']:.1%} | 높을수록 좋음 |",
        f"| F1 | Precision 과 Recall 의 조화평균 = 2PR ÷ (P + R) · 둘 중 낮은 쪽에 끌려감 · 이번 {m['f1']:.3f} | 1에 가까울수록 좋음 |",
        "| ROC-AUC | 수락 기사를 거절 기사보다 높게 점수 매길 확률 · 0.5 = 동전 던지기 · 1.0 = 완벽 (임계값과 무관) | 1에 가까울수록 좋음 |",
        f"| PR-AUC | 모든 임계값에서의 Precision 평균 (Average Precision) · 아무것도 모르면 검증셋 수락 비율({val_rate:.1%})쯤 나옴 | 1에 가까울수록 좋음 |",
        "| LogLoss | 예측 확률이 실제 결과와 얼마나 어긋나는지 (확신하고 틀리면 크게 벌점) | 0에 가까울수록 좋음 |",
        "| 하한선 (평균 수락률 일괄 예측) | 모델 없이 모두에게 같은 확률을 준 경우 — 모델이 이보다 나아야 의미 있음 | - |",
        "| 피처 중요도 (gain) | 모델이 판단할 때 그 항목(상차 거리 · 운임 등)이 기여한 비중 | - |",
        "",
        "- 해석 주의: 수락 비율이 높은 데이터라 '전부 제안'만 해도 Recall 100% · F1 이 높게 나온다. "
        "모델의 실력은 하한선과 ROC-AUC · LogLoss 차이로 비교하는 것이 정확하다.",
    ]


# ------------------------------------------------------------------ 실행 · 결과 저장
def save_result_block(text: str) -> None:
    path = Path(__file__).resolve()
    src = path.read_text(encoding="utf-8")
    idx = src.rfind("\n" + RESULT_MARK)
    code = (src[:idx] if idx != -1 else src).rstrip()
    if "def evaluate(" not in code:          # 안전장치: 코드가 잘려 나가는 일이 없게
        print("결과 저장을 건너뜀 (파일 구조를 확인하세요)")
        return
    body = text.replace("'''", "’’’").strip()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    path.write_text(f"{code}\n\n\n{RESULT_MARK}\n'''\n저장 {stamp}\n\n{body}\n'''\n", encoding="utf-8")
    print(f"결과를 이 파일 맨 아래에 저장: {path.relative_to(ROOT).as_posix()}")


def _has_libs() -> bool:
    import importlib
    try:
        importlib.import_module("lightgbm")
        importlib.import_module("numpy")
        return True
    except Exception:  # noqa: BLE001 — 깨진 설치 포함
        return False


def run_in_docker() -> int:
    """이 PC 에 lightgbm 이 없으면 도커 이미지 안에서 이 파일을 다시 실행한다 (결과 저장은 이 PC 에서)."""
    try:
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
            print(f"도커 이미지 {IMAGE} 가 없습니다. 수정본 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
            return 1
    except FileNotFoundError:
        print("lightgbm 도 docker 도 없습니다. Docker Desktop 을 켜거나 'pip install lightgbm numpy' 후 다시 실행하세요.")
        return 1
    print(f"이 PC 에 lightgbm 이 없어 도커 이미지({IMAGE})에서 실행합니다...")
    cmd = ["docker", "run", "--rm", "--network", "none", "-e", "PYTHONPATH=/src", "-e", "BAECHA_DB=0",
           "-e", "PYTHONIOENCODING=utf-8", "-v", f"{ROOT.as_posix()}:/src", "-w", "/src",
           IMAGE, "python", "tests/eval_matching_lgbm.py", "--print-only"]
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        return r.returncode
    text = r.stdout.split("<<<RESULT>>>")[-1].strip()
    print("\n" + for_terminal(text) + "\n")
    save_result_block(text)
    return 0


def main(argv: list[str]) -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("BAECHA_DB", "0")
    if not _has_libs():
        return run_in_docker()
    text = evaluate()
    if "--print-only" in argv:                # 도커 안 실행: 출력만 (파일 저장은 바깥에서)
        print("<<<RESULT>>>\n" + text)
        return 0
    print("\n" + for_terminal(text) + "\n")
    save_result_block(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


# ===== 마지막 실행 결과 (▷ 실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) =====
'''
저장 2026-11-19 14:37

## 매칭 AI (LightGBM 기사 수락 예측) 성능 지표

- 데이터: 가상 제안 8,000건 (시드 2026) · 학습 6,400건 · 검증 1,600건 (검증 수락 1,229건 · 수락률 76.8%)
- 모델: LightGBM binary · 피처 12개 · 단조 제약 · 자동 제안 임계값 0.75

| 모델 | ROC-AUC | PR-AUC | LogLoss | Precision@0.75 | Recall@0.75 | F1@0.75 |
|---|---:|---:|---:|---:|---:|---:|
| LightGBM (서비스 모델) | **0.850** | 0.941 | 0.373 | 89.4% | 86.2% | 0.877 |
| 휴리스틱 모의 스코어러 (대체용) | **0.834** | 0.933 | 0.486 | 96.0% | 25.1% | 0.397 |
| 평균 수락률 일괄 예측 (하한선) | **0.500** | 0.769 | 0.542 | 76.8% | 100.0% | 0.869 |

### 임계값 0.75 혼동 행렬 (LightGBM · 검증 1,600건)

| | 실제 수락 | 실제 거절 |
|---|---:|---:|
| 제안함 (점수 ≥ 0.75) | 1,059 (TP) | 126 (FP) |
| 제안 안 함 | 170 (FN) | 245 (TN) |

- 제안 대상 비율 74.1% · 제안한 기사 중 실제 수락 89.4% → 거절률 약 10.6%

### 임계값별 비교 (LightGBM)

| 임계값 | 제안 비율 | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|
| 0.5 | 85.7% | 86.1% | 96.0% | 0.908 |
| 0.6 | 82.2% | 87.2% | 93.4% | 0.902 |
| 0.7 | 77.6% | 88.6% | 89.5% | 0.890 |
| 0.75 | 74.1% | 89.4% | 86.2% | 0.877 |
| 0.8 | 68.6% | 90.7% | 81.0% | 0.856 |
| 0.9 | 51.4% | 93.1% | 62.2% | 0.746 |

### 피처 중요도 (gain 비중 상위 5)

- pickup_km (상차지까지 거리): 59.2%
- fare_gap (적정 운임 대비 제시 운임): 10.6%
- trip_km (운행 거리): 5.2%
- rating (기사 평점): 5.2%
- cf_score (권역 익숙도): 4.5%

- 실행 0.4초 · 가상(시뮬레이션) 데이터 기준 — 실운행 수락 기록으로 재학습하면 수치가 달라질 수 있음

### 용어 정리

| 용어 | 뜻 | 좋은 방향 |
|---|---|:---:|
| 수락 확률 (점수) | 모델이 예측한 '이 기사가 이 화물 제안을 수락할 확률' 0~1 | - |
| 임계값 (0.75) | 점수가 이 값 이상인 기사에게만 자동 제안 (높일수록 신중하게 · 적게 제안) | - |
| 학습셋 / 검증셋 | 모델을 가르친 데이터 / 학습에 쓰지 않고 성능을 잰 데이터 (처음 보는 문제로 시험) | - |
| TP (참 양성) | 제안했고 실제로 수락 — 맞힌 제안 | 많을수록 좋음 |
| FP (거짓 양성) | 제안했는데 거절 — 헛제안 (기사 · 화주 시간 낭비) | 적을수록 좋음 |
| FN (거짓 음성) | 제안 안 했는데 실제로는 수락했을 기사 — 놓친 기사 | 적을수록 좋음 |
| TN (참 음성) | 제안 안 했고 실제로도 거절 — 잘 걸러낸 경우 | 많을수록 좋음 |
| 혼동 행렬 | 위 TP · FP · FN · TN 4칸을 표로 정리한 것 | - |
| Precision (정밀도) | 제안한 기사 중 실제 수락 비율 = TP ÷ (TP + FP) · 이번 89.4% | 높을수록 좋음 |
| Recall (재현율) | 실제 수락할 기사 중 모델이 찾아낸 비율 = TP ÷ (TP + FN) · 이번 86.2% | 높을수록 좋음 |
| F1 | Precision 과 Recall 의 조화평균 = 2PR ÷ (P + R) · 둘 중 낮은 쪽에 끌려감 · 이번 0.877 | 1에 가까울수록 좋음 |
| ROC-AUC | 수락 기사를 거절 기사보다 높게 점수 매길 확률 · 0.5 = 동전 던지기 · 1.0 = 완벽 (임계값과 무관) | 1에 가까울수록 좋음 |
| PR-AUC | 모든 임계값에서의 Precision 평균 (Average Precision) · 아무것도 모르면 검증셋 수락 비율(76.8%)쯤 나옴 | 1에 가까울수록 좋음 |
| LogLoss | 예측 확률이 실제 결과와 얼마나 어긋나는지 (확신하고 틀리면 크게 벌점) | 0에 가까울수록 좋음 |
| 하한선 (평균 수락률 일괄 예측) | 모델 없이 모두에게 같은 확률을 준 경우 — 모델이 이보다 나아야 의미 있음 | - |
| 피처 중요도 (gain) | 모델이 판단할 때 그 항목(상차 거리 · 운임 등)이 기여한 비중 | - |

- 해석 주의: 수락 비율이 높은 데이터라 '전부 제안'만 해도 Recall 100% · F1 이 높게 나온다. 모델의 실력은 하한선과 ROC-AUC · LogLoss 차이로 비교하는 것이 정확하다.

### 결과 해설

**1. 모델 비교표**

- LightGBM(서비스 모델)이 대부분 지표에서 가장 좋다. 하한선(모델 없이 모두 같은 점수) 대비 ROC-AUC 0.500 → **0.850** (수락 · 거절 기사를 구분하는 힘), LogLoss 0.542 → **0.373** (예측 확률이 실제에 더 가까움).
- 휴리스틱 대체 스코어러는 ROC-AUC 0.834 로 순서 매기기는 비슷하지만, 0.75 기준으로는 너무 깐깐해 Recall 25.1% — 받았을 기사의 상당수를 놓친다 (LightGBM 을 못 쓸 때만 대체용).
- 하한선의 F1(0.869)이 높아 보이는 것은 '전부 제안'이라 Recall 이 100% 가 되기 때문이지 실력이 아니다 → 모델 실력은 ROC-AUC · LogLoss 로 비교한다.

**2. 혼동 행렬 (임계값 0.75)**

- 제안한 기사 1,185명 중 1,059명이 수락 → Precision 89.4% (거절 126건 · 약 10.6%).
- 실제로 수락할 기사 1,229명 중 1,059명에게 제안 → Recall 86.2% (놓친 기사 170명).
- 거절할 기사 371명 중 245명(66%)에게는 미리 제안하지 않아 헛제안을 막았다.

**3. 임계값별 비교 — 왜 0.75 인가**

| 임계값 | 제안 비율 | 거절률 (100% − Precision) | 놓친 기사 (100% − Recall) |
|---:|---:|---:|---:|
| 0.5 | 85.7% | 13.9% | 4.0% |
| 0.6 | 82.2% | 12.8% | 6.6% |
| 0.7 | 77.6% | 11.4% | 10.5% |
| 0.75 | 74.1% | 10.6% | 13.8% |
| 0.8 | 68.6% | 9.3% | 19.0% |
| 0.9 | 51.4% | 6.9% | 37.8% |

- 임계값을 올리면 확실한 기사에게만 제안 → 거절은 줄지만 놓치는 기사가 늘고, 내리면 그 반대다.
- F1 만 보면 0.5(0.908)가 가장 높지만, 거절은 기사 · 화주 시간 낭비와 재배차 지연으로 비용이 크고, 놓친 기사는 다음 후보에게 제안이 넘어가면 되므로 손해가 작다 → 거절을 줄이는 보수적 기준을 쓴다.
- 0.75 에서 거절률 10.6% · 놓친 기사 13.8% — 이보다 올리면 거절은 조금 줄지만 놓치는 기사가 빠르게 늘어난다. KPI 배차 거절률 목표(15~20% 미만)도 만족.

**4. 피처 중요도**

- 수락 여부를 가장 크게 좌우하는 것은 상차지까지 거리(59.2%), 다음이 적정 운임 대비 제시 운임(10.6%) → 가까운 공차 기사에게 적정 운임으로 먼저 제안하는 매칭 방식의 근거.
- 단, 가상 기사의 행동 규칙을 만들 때 상차 거리를 가장 크게 반영했으므로 모델이 그 규칙을 배운 결과다. 실제 운행 기록으로 재학습하면 순위가 달라질 수 있다.

**발표용 한 줄**

- "AI 가 제안한 기사 10명 중 약 9명이 수락했고, 수락할 기사의 86%를 찾아냈습니다. 모델 없이 찍는 것 대비 ROC-AUC 가 0.50 → 0.85 로 올라갑니다. (가상 데이터 기준)"
'''
