"""
상부 허용하중 안전 여유(R6 · TOP_LOAD_MARGIN) 전후 비교 — 같은 시드 · 같은 주문 8,008건을 여유값만 바꿔 두 번 돌린다.

비교하는 것
  - 입력 오차 반영 실제 위험률: 화주 신고 무게가 실제와 −5 ~ +10% 다를 때, 배차된 적재 계획을 '실제 무게'로
    다시 검증해서 물리 한계(허용하중 100% · 최대 적재중량 · 축중량)를 넘는 비율
  - 그 대가: 품목 주문 유효 적재율 · 매칭 성공률 · 거절률 · 공차 단축률 · 배차 지연 P95

계산은 도커 이미지(baecha-app:dev)에서 하고, 이 스크립트는 실행 · 진행 표시 · 비교표만 만든다 (표준 라이브러리만 사용).
KPI 기준 파일(results/benchmark_large.json · backend/data/*)은 건드리지 않는다 — 결과는 results/margin_compare/ 에만 저장.

직접 실행하지 않고 3_margin_compare.py · 4_show_results.py 가 불러 쓴다. 옵션을 직접 주려면 (수정본 폴더에서)
    python stress_8k/3_margin_compare.py --parallel         # 두 개 동시에 (약 8~10분, 지연 P95 는 순차 실행 쪽이 정확)
    python stress_8k/3_margin_compare.py --margins 0 0.05 0.10 0.15   # 여유값 여러 개
    python stress_8k/3_margin_compare.py --quick            # 빠른 확인 (하루 1,001건 · 약 1~2분)

결과 (stress_8k/results/)
    margin_compare/margin_<NN>.json      여유값별 원본 결과
    margin_compare/summary.md            비교표 (보고서에 붙여 넣기용)
    logs/margin_<NN>.log                 실행 로그 (커밋 안 함)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent                # stress_8k 폴더
ROOT = HERE.parent                                      # 수정본 폴더 (도커에 /src 로 연결)
OUT_DIR = HERE / "results" / "margin_compare"
LOGS = HERE / "results" / "logs"
IMAGE = "baecha-app:dev"

if hasattr(sys.stdout, "reconfigure"):                   # 윈도우 콘솔에서 한글 · 기호 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


LAST_TEXT: str | None = None   # 마지막으로 출력한 비교표 (실행 파일 맨 아래 결과 블록에 저장)


def _tag(m: float) -> str:
    return f"{round(m * 100):02d}"


def _out(m: float) -> Path:
    return OUT_DIR / f"margin_{_tag(m)}.json"


# ---------------------------------------------------------------- 실행
def _command(m: float, a, single_thread: bool) -> list[str]:
    days, orders, drivers = (1, 1001, 400) if a.quick else (a.days, a.orders, a.drivers)
    out = _out(m).relative_to(ROOT).as_posix()
    args = ["tests/stress_test.py", "--seed", str(a.seed), "--days", str(days), "--orders", str(orders),
            "--drivers", str(drivers), "--batch", "8", "--target", str(days * orders), "--max-minutes", "60",
            "--expire-hours", "3", "--detail", "--out", out]
    if a.local:
        return [sys.executable, *args]
    env = ["-e", "TZ=Asia/Seoul", "-e", "BAECHA_DB=0", "-e", "PYTHONIOENCODING=utf-8", "-e", f"TOP_LOAD_MARGIN={m}"]
    if single_thread:  # 동시에 돌릴 때 CPU 를 서로 다투지 않게
        env += ["-e", "OMP_NUM_THREADS=1", "-e", "OPENBLAS_NUM_THREADS=1", "-e", "MKL_NUM_THREADS=1"]
    return ["docker", "run", "--rm", "--network", "none", *env, "-v", f"{ROOT.as_posix()}:/src", "-w", "/src",
            IMAGE, "python", *args]


def run_one(m: float, a, parallel: bool) -> tuple[float, bool, float]:
    out, log = _out(m), LOGS / f"margin_{_tag(m)}.log"
    env = {**os.environ, "BAECHA_DB": "0", "PYTHONIOENCODING": "utf-8", "TZ": "Asia/Seoul", "TOP_LOAD_MARGIN": str(m)}
    t0 = time.time()
    label = f"[여유 {m:.0%}]"
    print(f"  {label} 시작", flush=True)
    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(_command(m, a, parallel), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env)
        for line in p.stdout:
            f.write(line)
            if "일차 완료" in line or "실행:" in line or "결과:" in line or "Error" in line:
                print(f"  {label} {line.strip()}", flush=True)
        p.wait()
    ok = p.returncode == 0 and out.exists() and out.stat().st_mtime >= t0
    if not ok:
        print(f"  {label} 실패 (종료 코드 {p.returncode}) — 로그: {log.relative_to(ROOT).as_posix()}", flush=True)
    return m, ok, time.time() - t0


# ---------------------------------------------------------------- 비교표
def _load(m: float) -> dict | None:
    try:
        return json.loads(_out(m).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pct(v, digits=1):
    return "-" if v is None else f"{v:.{digits}f}%"


ROWS = [
    # (구분, 이름, 값 함수, 낮을수록 좋은가)
    ("안전", "**입력 오차 반영 실제 위험률**", lambda d, r: _pct(r.get("risk_rate_pct"), 2)),
    ("안전", "　상부 허용하중 초과 (실제 무게 · 허용하중 100% 기준)",
     lambda d, r: f"{r.get('top_load', 0):,}/{r.get('checked', 0):,}건"),
    ("안전", "　초과 정도 ≤2% · 2~5% · 5~10% · >10%",
     lambda d, r: " · ".join(str(v) for v in (r.get("top_load_excess") or {}).values()) or "-"),
    ("안전", "　최대 초과", lambda d, r: f"+{r.get('top_load_worst_pct', 0)}%"),
    ("안전", "　적재중량 초과 · 축중량 위반", lambda d, r: f"{r.get('overweight', 0)} · {r.get('axle', 0)}건"),
    ("안전", "신고값 기준 재검증 위반 (KPI)", lambda d, r: f"{d.get('packing_violations', 0)}/{d.get('packing_checked', 0):,}건"),
    ("적재", "품목 주문 유효 적재율 (평균)", lambda d, r: _pct(r.get("avg_item_util_pct"))),
    ("매칭", "매칭 성공률", lambda d, r: _pct(d.get("match_rate_pct"))),
    ("매칭", "배차 거절률", lambda d, r: _pct(d.get("rejection_rate_pct"))),
    ("매칭", "공차 거리 단축률 (최단거리 대비)", lambda d, r: _pct(d.get("deadhead_vs_nearest_pct"))),
    ("매칭", "배차 지연 P95", lambda d, r: f"{d.get('dispatch_latency_p95_sec', 0):.2f}초"),
    ("참고", "주문 생성 시도 · 실행 시간", lambda d, r: f"{d.get('attempts', 0):,}건 · {d.get('elapsed_sec', 0):.0f}초"),
]


def table(margins: list[float]) -> list[str] | None:
    data = {m: _load(m) for m in margins}
    have = [m for m in margins if data[m]]
    if not have:
        return None
    first = data[have[0]]
    err = (first.get("input_error") or {}).get("error_range_pct") or [-5, 10]
    lines = [f"> 시드 {first.get('seed')} · 주문 {first.get('attempts', 0):,}건 · 신고 무게 대비 실제 "
             f"{err[0]:+g} ~ {err[1]:+g}% 오차 · 같은 조건에서 상부 허용하중 여유만 바꿈",
             "",
             "| 구분 | 지표 | " + " | ".join(f"여유 {m:.0%}" + (" (허용하중 100%까지)" if m == 0 else
                                               f" (허용하중 {1 - m:.0%}까지)") for m in have) + " |",
             "|---|---|" + "---:|" * len(have)]
    for area, name, fn in ROWS:
        cells = []
        for m in have:
            d = data[m]
            try:
                cells.append(fn(d, d.get("input_error") or {}))
            except (TypeError, ValueError, KeyError):
                cells.append("-")
        lines.append(f"| {area} | {name} | " + " | ".join(cells) + " |")
    missing = [m for m in margins if not data[m]]
    if missing:
        lines += ["", "결과 없음: " + ", ".join(f"여유 {m:.0%}" for m in missing)]
    lines += ["",
              "- **입력 오차 반영 실제 위험률**: 엔진은 신고 무게로 적재 계획을 세우므로 신고값 기준 위반은 항상 0. "
              "실제 무게로 다시 재면 상부 하중이 허용하중을 넘는 상자가 생길 수 있고, 여유가 그 차이를 흡수한다.",
              "- 여유를 키우면 위험률은 내려가지만, 위로 쌓을 수 있는 무게가 줄어 적재율 · 차종 선택에 영향을 준다."]
    return lines


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="상부 허용하중 안전 여유 전후 비교 (8,008건)")
    ap.add_argument("--margins", type=float, nargs="+", default=[0.0, 0.10], help="비교할 여유값 (0.10 = 10%%)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--orders", type=int, default=1001, help="하루 주문 수")
    ap.add_argument("--drivers", type=int, default=400)
    ap.add_argument("--quick", action="store_true", help="하루 1,001건으로 빠르게 확인")
    ap.add_argument("--parallel", action="store_true", help="여유값별 실행을 동시에")
    ap.add_argument("--show", action="store_true", help="실행 없이 마지막 결과만 표시")
    ap.add_argument("--local", action="store_true", help="도커 대신 로컬 파이썬으로 실행")
    a = ap.parse_args(argv)
    margins = list(dict.fromkeys(round(m, 4) for m in a.margins))
    if any(not 0 <= m < 1 for m in margins):
        print("여유값은 0 이상 1 미만이어야 합니다 (예: 0.10)")
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    if not a.show:
        from _kpi import check_docker  # 같은 폴더 (번호 붙은 실행 파일이 이 폴더를 경로에 넣음)
        if not a.local and not check_docker():
            return 1
        scale = "하루 1,001건" if a.quick else f"{a.days}일 × 하루 {a.orders:,}건 = {a.days * a.orders:,}건"
        print(f"상부 허용하중 여유 {', '.join(f'{m:.0%}' for m in margins)} 비교 · 시드 {a.seed} · {scale} · "
              f"{'동시 실행' if a.parallel and len(margins) > 1 else '순서대로 실행'}", flush=True)
        t0 = time.time()
        if a.parallel and len(margins) > 1:
            with ThreadPoolExecutor(len(margins)) as ex:
                results = list(ex.map(lambda m: run_one(m, a, True), margins))
        else:
            results = [run_one(m, a, False) for m in margins]
        for m, ok, sec in results:
            print(f"  여유 {m:.0%}: {'완료' if ok else '실패'} ({sec / 60:.1f}분)")
        print(f"전체 {(time.time() - t0) / 60:.1f}분\n")

    lines = table(margins)
    if not lines:
        print("결과가 없습니다. 먼저 stress_8k/3_margin_compare.py 를 실행하세요.")
        return 1
    global LAST_TEXT
    LAST_TEXT = "## 상부 허용하중 안전 여유 전후 비교\n\n" + "\n".join(lines)
    print("\n".join(lines))
    (OUT_DIR / "summary.md").write_text("# 상부 허용하중 안전 여유 전후 비교\n\n" + "\n".join(lines) + "\n",
                                        encoding="utf-8")
    print(f"\n비교표 저장: {(OUT_DIR / 'summary.md').relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
