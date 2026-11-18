"""
적재 전용 대량 스트레스 테스트 실행 · 표 정리 (계산 본체: tests/packing_stress.py · 도커 이미지에서 실행).

직접 실행하지 않고 5_packing_stress.py · 4_show_results.py 가 불러 쓴다. 옵션을 직접 주려면 (수정본 폴더에서)
    python stress_8k/5_packing_stress.py --cases 30000                 # 건수 늘리기
    python stress_8k/5_packing_stress.py --margins 0 0.1               # 상부 허용하중 여유값
    python stress_8k/5_packing_stress.py --errors=-5:5,-10:20          # 신고 대비 실제 무게 오차 범위들 (%) — '=' 붙여서
    python stress_8k/5_packing_stress.py --bad-rate 0.3                # 위반 화물 비율
    python stress_8k/5_packing_stress.py --quick                       # 빠른 확인 (500건)

결과 (stress_8k/results/)
    packing/packing_stress.json · packing/summary.md     원본 집계 · 표
    logs/packing_stress.log                              실행 로그 (커밋 안 함)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "results" / "packing" / "packing_stress.json"
LOGS = HERE / "results" / "logs"
IMAGE = "baecha-app:dev"
LAST_TEXT: str | None = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _pct(v, d=1):
    return "-" if v is None else f"{v:.{d}f}%"


def table(d: dict) -> list[str]:
    ms = d["margins"]
    bm = {float(k): v for k, v in d["by_margin"].items()}
    cols = [bm[m] for m in ms]
    head = "| 구분 | 지표 | " + " | ".join(f"여유 {m:.0%}" for m in ms) + " |"
    sep = "|---|---|" + "---:|" * len(ms)

    def row(area, name, f):
        return f"| {area} | {name} | " + " | ".join(f(c) for c in cols) + " |"

    lines = [f"> 적재 케이스 {d['cases']:,}건 (정상 {d['clean_cases']:,} · 위반 {d['bad_cases']:,}) · 시드 {d['seed']} · "
             f"{d['elapsed_sec']:.0f}초 · 프로세스 {d['workers']}개 · {d['generated_at']}",
             "> 같은 화물을 상부 허용하중 여유값만 바꿔 다시 적재 · 입력 오차는 여유값과 상관없이 같은 값", "",
             "### 정상 화물 — 적재 결과", "", head, sep,
             row("적재", "등록 가능 (전량 적재 · 규칙 통과)", lambda c: f"{c['ok']:,}/{c['clean']:,} ({100 * c['ok'] / c['clean']:.1f}%)"),
             row("적재", "신고값 기준 재검증 위반 (KPI 0%)", lambda c: f"{c['declared_fail']}건"),
             row("적재", "평균 부피 · 중량 적재율", lambda c: f"{_pct(c['avg_vol_pct'])} · {_pct(c['avg_wt_pct'])}"),
             row("대가", f"여유 {ms[0]:.0%} 대비 더 큰 차종 필요", lambda c: f"{c['bigger_truck_vs_base']:,}건"),
             row("대가", f"여유 {ms[0]:.0%} 대비 새로 등록 불가", lambda c: f"{c['lost_vs_base']:,}건"),
             row("속도", "적재 계산 평균 · p95", lambda c: f"{c['ms_avg']:.0f} · {c['ms_p95']:.0f}ms")]
    lines += ["", "### 실제 무게 재검증 — 현실 위험률 (물리 한계: 최대 적재중량 · 상부 허용하중 100% · 축중량)", "", head, sep]
    for lo, hi in d["errors"]:
        k = f"{lo:g}:{hi:g}"
        lines.append(row(f"{lo:+g}~{hi:+g}%", "**위험률**", lambda c, k=k: f"**{_pct(c['real'][k]['rate'], 2)}** "
                                                                         f"({c['real'][k]['any']:,}/{c['real'][k]['checked']:,})"))
        lines.append(row("", "　상부 하중 · 중량 · 축중량 초과",
                         lambda c, k=k: f"{c['real'][k]['top']} · {c['real'][k]['over']} · {c['real'][k]['axle']}"))
        lines.append(row("", "　최대 초과 (≤2 · 2~5 · 5~10 · >10%)",
                         lambda c, k=k: f"+{c['real'][k]['worst']}% (" + " · ".join(
                             str(c['real'][k]['buckets'].get(b, 0)) for b in ("≤2%", "2~5%", "5~10%", ">10%")) + ")"))
    labels = d.get("bad_labels") or {}
    lines += ["", "### 위반 화물 — 등록 단계 차단", "", head, sep]
    for kind in labels:
        lines.append(row("위반", labels[kind], lambda c, kind=kind: f"{c['bad'][kind]['blocked']}/{c['bad'][kind]['injected']}"))
    tot = lambda c: (sum(v["blocked"] for v in c["bad"].values()), sum(v["injected"] for v in c["bad"].values()))  # noqa: E731
    lines.append(row("위반", "**합계 차단률**", lambda c: f"**{tot(c)[0]:,}/{tot(c)[1]:,}"
                                                     f" ({100 * tot(c)[0] / max(1, tot(c)[1]):.1f}%)**"))
    first = cols[-1]
    if first["fail_reasons"]:
        lines += ["", f"정상 화물 등록 불가 사유 (여유 {ms[-1]:.0%}): " +
                  " · ".join(f"{k} {v}건" for k, v in first["fail_reasons"].items())]
    lines += ["",
              "- **현실 위험률**: 엔진은 신고 무게로 계획을 세우므로 신고값 기준 위반은 0이어야 한다. 실제 무게가 다르면 "
              "아래 상자가 받는 하중이 허용하중을 넘을 수 있고, 여유가 그 차이를 흡수한다.",
              "- **대가**: 여유를 키우면 위로 쌓을 수 있는 무게가 줄어 더 큰 차가 필요하거나 등록이 안 되는 화물이 생긴다."]
    return lines


def show() -> int:
    global LAST_TEXT
    try:
        d = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("결과가 없습니다. 먼저 stress_8k/5_packing_stress.py 를 실행하세요.")
        return 1
    lines = table(d)
    LAST_TEXT = "## 적재 전용 대량 스트레스 테스트\n\n" + "\n".join(lines)
    print("\n" + LAST_TEXT + "\n")
    (OUT.parent / "summary.md").write_text(LAST_TEXT + "\n", encoding="utf-8")
    print(f"표 저장: {(OUT.parent / 'summary.md').relative_to(ROOT).as_posix()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="적재 전용 대량 스트레스 테스트")
    ap.add_argument("--cases", type=int, default=10000, help="케이스 수 (기본 10,000)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--margins", type=float, nargs="+", default=[0.0, 0.05, 0.10, 0.15], help="상부 허용하중 여유값들")
    ap.add_argument("--errors", default="-5:5,-5:10,-15:15", help="신고 대비 실제 무게 오차 범위들 (%%) 예: --errors=-5:5,-5:10")
    ap.add_argument("--bad-rate", type=float, default=0.15, help="위반 화물 비율 (기본 0.15)")
    ap.add_argument("--workers", type=int, default=0, help="동시 프로세스 수 (0 = 도커가 쓸 수 있는 CPU 전부)")
    ap.add_argument("--quick", action="store_true", help="빠른 확인 (500건)")
    ap.add_argument("--show", action="store_true", help="실행 없이 마지막 결과만 표로")
    ap.add_argument("--local", action="store_true", help="도커 대신 로컬 파이썬")
    a = ap.parse_args(argv)
    if a.show:
        return show()
    from _kpi import check_docker
    if not a.local and not check_docker():
        return 1
    cases = 500 if a.quick else a.cases
    args = ["tests/packing_stress.py", "--cases", str(cases), "--seed", str(a.seed), "--margins", *map(str, a.margins),
            f"--errors={a.errors}", "--bad-rate", str(a.bad_rate), "--workers", str(a.workers),
            "--out", OUT.relative_to(ROOT).as_posix()]
    if a.local:
        cmd = [sys.executable, *args]
    else:
        cmd = ["docker", "run", "--rm", "--network", "none", "-e", "TZ=Asia/Seoul", "-e", "BAECHA_DB=0",
               "-e", "PYTHONIOENCODING=utf-8", "-e", "OMP_NUM_THREADS=1",
               "-v", f"{ROOT.as_posix()}:/src", "-w", "/src", IMAGE, "python", *args]
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "packing_stress.log"
    t0 = time.time()
    env = {**os.environ, "BAECHA_DB": "0", "PYTHONIOENCODING": "utf-8", "TZ": "Asia/Seoul"}
    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env=env)
        for line in p.stdout:
            f.write(line)
            if line.startswith(("적재 스트레스", "  진행", "완료", "저장:")) or "Error" in line:
                print(line.rstrip(), flush=True)
        p.wait()
    if p.returncode != 0 or not OUT.exists() or OUT.stat().st_mtime < t0:
        print(f"실패 (종료 코드 {p.returncode}) — 로그: {log.relative_to(ROOT).as_posix()}")
        return 1
    return show()


if __name__ == "__main__":
    sys.exit(main())
