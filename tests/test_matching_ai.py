"""
매칭 AI (LightGBM 기사 수락 예측) 점검 — 랜덤 시드 비교 · 임계값 · 재현성 · 단조 제약 · 매칭 연동 (서버 없이 · 약 10초)
    실행: 수정본 폴더에서  python tests/test_matching_ai.py   (pytest 가 tests/test_suites.py 로 함께 실행)

tests/eval_matching_lgbm.py 는 발표용 수치(시드 2026 한 번)를 뽑는다. 이 파일은 그 수치가 '우연히 잘 나온 한 번'이
아닌지(시드를 바꿔도 비슷한지), 임계값 0.75 가 시드와 상관없이 KPI 를 지키는지, 실제 매칭이 임계값을 지키는지 본다.

1. 랜덤 시드 5개 (가상 데이터 · 모델 시드를 함께 바꿔 매번 새로 학습)
   - 시드마다 ROC-AUC ≥ 0.80 · 시드 간 편차(표준편차) ≤ 0.02
   - 시드마다 하한선(평균 수락률 일괄 예측)보다 LogLoss 가 낮음 · 휴리스틱 대체 스코어러보다도 낮음
   - 임계값 0.75: 시드마다 거절률(1 − Precision) ≤ 15% (KPI 목표) · Recall ≥ 75%
   - 임계값을 올릴수록 제안 비율 · Recall 은 줄고(단조), 0.9 의 Precision ≥ 0.5 의 Precision
2. 재현성: 같은 시드로 두 번 학습하면 예측이 같음 · 저장된 서비스 모델 파일 = 지금 코드로 학습한 모델
3. 단조 제약 (업무 상식): 상차 거리가 멀수록 수락 확률이 오르지 않음 · 운임이 높을수록 · 평점이 높을수록 내려가지 않음
4. 매칭 연동: 후보마다 lgb_pass = (수락 확률 ≥ 0.75) · 미달 기사는 자동 제안 후보에서 빠짐 ·
   임계값을 못 넘으면 실패 사유에 '임계값' 표시
학습은 메모리에서만 한다 (backend/data/ml_ranker.txt 를 덮어쓰지 않음).
"""

from statistics import mean, pstdev

from _check import check, finish, section

import numpy as np

from backend.engines.matcher import ml_ranker as mr
from eval_matching_lgbm import SWEEP, THRESHOLD, at_threshold, log_loss, roc_auc

SEEDS = [2026, 7, 11, 42, 2027]
ORIG_GEN, ORIG_SEED = mr.generate_synthetic, mr.SEED


def train_with_seed(seed: int):
    """ml_ranker.train() 을 그대로 쓰되 가상 데이터 시드 · LightGBM 시드만 바꾼다 (서비스 코드는 건드리지 않음)."""
    mr.SEED = seed
    mr.generate_synthetic = lambda n=mr.N_SAMPLES, seed_=None: ORIG_GEN(n, seed)
    try:
        model = mr.train(save=False)["model"]
    finally:
        mr.SEED, mr.generate_synthetic = ORIG_SEED, ORIG_GEN
    X, y = ORIG_GEN(mr.N_SAMPLES, seed)
    cut = int(len(X) * 0.8)
    return model, X[cut:], y[cut:], sum(y[:cut]) / cut


# ====================================================================== 1
section(f"1. 랜덤 시드 {len(SEEDS)}개 비교 (가상 데이터 · 모델 시드 함께 변경)")
rows = []
for s in SEEDS:
    model, Xv, yv, base = train_with_seed(s)
    p = [float(v) for v in model.predict(np.array(Xv))]
    mock = [mr.mock_lgb_score(dict(zip(mr.FEATURES, x))) for x in Xv]
    m = at_threshold(yv, p, THRESHOLD)
    sweep = [at_threshold(yv, p, th) for th in SWEEP]
    rows.append({"seed": s, "auc": roc_auc(yv, p), "ll": log_loss(yv, p), "ll_base": log_loss(yv, [base] * len(yv)),
                 "ll_mock": log_loss(yv, mock), "prec": m["precision"], "rec": m["recall"], "sweep": sweep})

print("  시드  | ROC-AUC | LogLoss (하한선 · 휴리스틱) | 거절률@0.75 | Recall@0.75")
for r in rows:
    print(f"  {r['seed']:>5} |  {r['auc']:.3f}  | {r['ll']:.3f} ({r['ll_base']:.3f} · {r['ll_mock']:.3f})"
          f"      |   {1 - r['prec']:5.1%}    |   {r['rec']:.1%}")
aucs = [r["auc"] for r in rows]
print(f"  평균 ROC-AUC {mean(aucs):.3f} · 표준편차 {pstdev(aucs):.4f} · 범위 {min(aucs):.3f}~{max(aucs):.3f}")

check("시드마다 ROC-AUC ≥ 0.80", min(aucs) >= 0.80, f"최저 {min(aucs):.3f}")
check("시드 간 ROC-AUC 표준편차 ≤ 0.02 (우연히 잘 나온 한 번이 아님)", pstdev(aucs) <= 0.02, f"{pstdev(aucs):.4f}")
check("시드마다 LogLoss < 하한선(평균 수락률 일괄 예측)", all(r["ll"] < r["ll_base"] for r in rows))
check("시드마다 LogLoss < 휴리스틱 대체 스코어러", all(r["ll"] < r["ll_mock"] for r in rows))
check(f"임계값 {THRESHOLD}: 시드마다 거절률 ≤ 15% (KPI 배차 거절률 목표)", all(1 - r["prec"] <= 0.15 for r in rows),
      f"최고 {max(1 - r['prec'] for r in rows):.1%}")
check(f"임계값 {THRESHOLD}: 시드마다 Recall ≥ 75% (수락할 기사를 너무 많이 놓치지 않음)",
      all(r["rec"] >= 0.75 for r in rows), f"최저 {min(r['rec'] for r in rows):.1%}")
mono = all(all(a["offered"] >= b["offered"] and a["recall"] >= b["recall"] for a, b in zip(r["sweep"], r["sweep"][1:]))
           for r in rows)
check(f"임계값 {SWEEP[0]}→{SWEEP[-1]} 올릴수록 제안 비율 · Recall 감소 (시드마다)", mono)
check(f"임계값 {SWEEP[-1]} 의 Precision ≥ {SWEEP[0]} 의 Precision (시드마다 · 올리면 거절 감소)",
      all(r["sweep"][-1]["precision"] >= r["sweep"][0]["precision"] for r in rows))

# ====================================================================== 2
section("2. 재현성")
m1, Xv, _, _ = train_with_seed(2026)
m2, _, _, _ = train_with_seed(2026)
a, b = m1.predict(np.array(Xv)), m2.predict(np.array(Xv))
check("같은 시드로 두 번 학습 → 예측 동일 (deterministic)", float(np.max(np.abs(a - b))) < 1e-12,
      f"최대 차이 {float(np.max(np.abs(a - b))):.2e}")
mr._ensure()
if mr._model is not None:
    served = mr._model.predict(np.array(Xv))
    diff = float(np.max(np.abs(served - a)))
    check("저장된 서비스 모델(backend/data/ml_ranker.txt) = 지금 코드로 학습한 모델", diff < 1e-6,
          f"최대 차이 {diff:.2e} (다르면 python -m backend.engines.matcher.ml_ranker 로 다시 학습)")
else:
    check("서비스 모델 로드 (LightGBM)", False, mr.info().get("error", ""))

# ====================================================================== 3
section("3. 단조 제약 (업무 상식) — 서비스 모델")
base = {"pickup_km": 20.0, "util": 0.6, "fare_gap": 0.0, "trip_km": 150.0, "cold": 0.0, "urgent": 0.0,
        "accept_rate": 0.7, "cf_score": 0.6, "rating": 4.4, "rush_hour": 0.0, "weather": 0.0, "ds_ratio": 1.0}


def sweep_feature(name: str, values: list[float]) -> list[float]:
    return mr.predict([{**base, name: v} for v in values])


def non_increasing(xs):
    return all(a >= b - 1e-9 for a, b in zip(xs, xs[1:]))


km = sweep_feature("pickup_km", [2, 10, 20, 40, 60, 90, 130])
check("상차 거리가 멀수록 수락 확률이 오르지 않음", non_increasing(km), " → ".join(f"{v:.2f}" for v in km))
fare = sweep_feature("fare_gap", [-0.3, -0.15, 0.0, 0.15, 0.3])
check("운임이 적정가보다 높을수록 수락 확률이 내려가지 않음", non_increasing(fare[::-1]), " → ".join(f"{v:.2f}" for v in fare))
rating = sweep_feature("rating", [2.0, 3.0, 4.0, 4.5, 5.0])
check("기사 평점이 높을수록 수락 확률이 내려가지 않음", non_increasing(rating[::-1]), " → ".join(f"{v:.2f}" for v in rating))
check("수락 확률은 항상 0~1", all(0.0 <= v <= 1.0 for v in km + fare + rating))

# ====================================================================== 4
section(f"4. 매칭 연동 — 임계값 {THRESHOLD} 미달 기사는 자동 제안 후보에서 제외")
from backend.core.database import Store  # noqa: E402
from backend.services import matching_service as ms  # noqa: E402

st = Store()
pairs = []
for o in st.orders.values():
    if o["status"] != "MATCHING":
        continue
    rows4 = st._evaluate(o)
    pairs += [(o, r) for r in rows4 if r["fits"]]
check("평가할 화물 × 기사 쌍 준비", len(pairs) > 0, f"{len(pairs)}쌍")
check(f"후보마다 lgb_pass = (수락 확률 ≥ {ms.ML_THRESHOLD})",
      all(r["lgb_pass"] == (r["ml_prob"] >= ms.ML_THRESHOLD) for _, r in pairs))
low = [r for _, r in pairs if not r["lgb_pass"]]
ranked = [r for o in {id(o): o for o, _ in pairs}.values() for r in st._ranking(st._evaluate(o))]
check("자동 제안 후보에는 임계값 미달 기사가 없음", all(r["ml_prob"] >= ms.ML_THRESHOLD for r in ranked),
      f"후보 {len(ranked)}쌍 · 미달로 빠진 쌍 {len(low)}")

saved = ms.ML_THRESHOLD
ms.ML_THRESHOLD = 1.01          # 아무도 못 넘는 임계값 → 자동 제안 후보 0 · 실패 사유에 '임계값'
try:
    o = next(o for o, r in pairs if r["in_radius"] and not r["busy"] and not r["declined"])
    rows4 = st._evaluate(o)
    reasons = st._fail_reasons(o, rows4)
    check("임계값을 아무도 못 넘으면 자동 제안 후보 0명", not st._ranking(rows4))
    check("실패 사유에 'AI 수락 예측이 임계값 미만' 표시", any("임계값" in x for x in reasons), reasons[0][:70])
except StopIteration:
    check("반경 안 · 비어 있는 적재 가능 기사가 있는 화물 준비", False)
finally:
    ms.ML_THRESHOLD = saved

finish()
