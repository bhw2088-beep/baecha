"""
KPI 목표치 측정 (매칭 · 적재) — 주문 8,000건 이상 스트레스 테스트를 실행하고 결과를 KPI 표로 보여준다.

계산은 이미 빌드된 도커 이미지(baecha-app:dev)에서 하고, 이 스크립트는 실행 · 진행 표시 · 결과 정리만 한다
(파이썬 표준 라이브러리만 사용 → 로컬 가상환경이 없어도 동작). 실제 DB · 서버는 건드리지 않는다.

직접 실행하지 않고 같은 폴더의 번호 붙은 파일(1_kpi_8k.py · 2_seeds_8k.py · 4_show_results.py)이 불러 쓴다.
옵션을 직접 주려면 (저장소 최상위 폴더에서)
    python backend/tests/stress_tests/1_kpi_8k.py --days 10 --orders 1000 --drivers 500   # 규모 직접 지정 (10,000건)
    python backend/tests/stress_tests/2_seeds_8k.py --seeds 1 2 3 7 --parallel 2          # 시드 · 동시 실행 수 지정
    python backend/tests/stress_tests/1_kpi_8k.py --local                                  # 도커 없이 로컬 파이썬 (requirements 설치된 경우)

결과 파일 (backend/tests/stress_tests/results/)
    benchmark_large.json · large_run/*.csv   시드 7 (KPI 기준, stress_test.py --preset 8k 와 같음)
    seeds/seed<N>.json                       다른 시드
    seeds/summary.md                         KPI 표 · 시드별 표 (보고서에 붙여 넣기용)
    logs/*.log                               실행 로그 (커밋 안 함)
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

HERE = Path(__file__).resolve().parent                # backend/tests/stress_tests 폴더
BACKEND = HERE.parents[1]                             # backend 폴더 (명령 실행 위치)
ROOT = BACKEND.parent                                   # 저장소 최상위 (도커에 /src 로 연결)
RESULTS = HERE / "results"
RUNS = RESULTS / "seeds"
LOGS = RESULTS / "logs"
IMAGE = "baecha-backend:dev"
ALL_SEEDS = [1, 2, 3, 4, 5, 7, 11, 42]                  # 보고서 1-1 시드별 표의 시드

if hasattr(sys.stdout, "reconfigure"):                   # 윈도우 콘솔에서 한글 · 기호 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KPI_SEED = 7                                            # 관제 KPI 기준 시드 (--preset 8k)

# 매칭 · 적재 KPI (engines/kpi.py 의 키) — 이 스크립트가 측정 · 표시하는 대상
KPI_ROWS = [
    ("매칭", "dispatch_p95_sec", "배차 지연 P95", "< 3초"),
    ("매칭", "rejection_rate_pct", "배차 거절률", "< 15~20%"),
    ("매칭", "deadhead_vs_nearest_pct", "공차 거리 단축률 (단순 최단거리 대비)", "10~15% 단축"),
    ("적재", "packing_violation_pct", "과적 · 적재 오탐 발생률", "0%"),
]
STATUS = {"pass": "달성", "warn": "근접", "fail": "미달", "none": "측정 전"}


# ---------------------------------------------------------------- 실행
def _stress_args(seed: int, days: int, orders: int, drivers: int, out: str, csv_dir: str | None) -> list[str]:
    if seed == KPI_SEED and (days, orders, drivers) == (8, 1001, 400) and csv_dir:
        return ["-m", "tests.stress_tests.stress_test", "--preset", "8k"]   # KPI 기준 실행 (관제 /admin/kpi 사본까지 갱신)
    args = ["-m", "tests.stress_tests.stress_test", "--seed", str(seed), "--days", str(days), "--orders", str(orders),
            "--drivers", str(drivers), "--batch", "8", "--target", str(days * orders), "--max-minutes", "60",
            "--expire-hours", "3", "--detail", "--out", out]
    return args + (["--csv-dir", csv_dir] if csv_dir else [])


def _command(args: list[str], local: bool, single_thread: bool) -> list[str]:
    if local:
        return [sys.executable, *args]
    env = ["-e", "TZ=Asia/Seoul", "-e", "BAECHA_DB=0", "-e", "PYTHONIOENCODING=utf-8"]
    if single_thread:  # 여러 개를 동시에 돌릴 때 CPU 를 서로 다투지 않게 (LightGBM · numpy 스레드 1개)
        env += ["-e", "OMP_NUM_THREADS=1", "-e", "OPENBLAS_NUM_THREADS=1", "-e", "MKL_NUM_THREADS=1"]
    return ["docker", "run", "--rm", "--network", "none", *env, "-v", f"{ROOT.as_posix()}:/src", "-w", "/src/backend",
            IMAGE, "python", *args]


def run_one(seed: int, a, parallel: bool) -> tuple[int, Path | None, float]:
    is_kpi = seed == KPI_SEED
    out = RESULTS / "benchmark_large.json" if is_kpi else RUNS / f"seed{seed}.json"
    rel_out = out.relative_to(BACKEND).as_posix()
    csv_dir = "tests/stress_tests/results/large_run" if is_kpi else None
    cmd = _command(_stress_args(seed, a.days, a.orders, a.drivers, rel_out, csv_dir), a.local, parallel)
    log = LOGS / f"seed{seed}.log"
    t0 = time.time()
    env = {**os.environ, "BAECHA_DB": "0", "PYTHONIOENCODING": "utf-8", "TZ": "Asia/Seoul"}
    with open(log, "w", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env)
        for line in p.stdout:
            f.write(line)
            if "일차 완료" in line or "실행:" in line or "결과:" in line or "Error" in line:
                print(f"  [시드 {seed}] {line.strip()}", flush=True)
        p.wait()
    ok = p.returncode == 0 and out.exists() and out.stat().st_mtime >= t0
    if not ok:
        print(f"  [시드 {seed}] 실패 (종료 코드 {p.returncode}) — 로그: {log.relative_to(ROOT)}", flush=True)
    return seed, (out if ok else None), time.time() - t0


# ---------------------------------------------------------------- 결과 표시
def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def kpi_table(d: dict) -> list[str]:
    k = {r["key"]: r for r in d.get("kpi", [])}
    bc = d.get("bad_cargo") or {}
    inj, blk = sum((bc.get("injected") or {}).values()), sum((bc.get("blocked") or {}).values())
    lines = [f"| 영역 | 지표 | 목표 | 측정 (주문 {d.get('attempts', 0):,}건 · 시드 {d.get('seed')}) | 판정 |",
             "|---|---|---|---:|:---:|"]
    for area, key, name, target in KPI_ROWS:
        r = k.get(key) or {}
        lines.append(f"| {area} | {name} | {target} | **{r.get('display', '-')}** | {STATUS.get(r.get('status'), '-')} |")
    lines.append(f"| 적재 | 위반 화물 차단 (과적 · 편심 주입) | 100% | **{blk:,}/{inj:,}건** | "
                 f"{'달성' if inj and blk == inj else '미달'} |")
    lines.append(f"| 적재 | 정상 화물 오차단 | 0% | **{bc.get('clean_blocked', 0)}/{bc.get('clean_checked', 0):,}건** | "
                 f"{'달성' if not bc.get('clean_blocked') else '미달'} |")
    return lines


def detail_lines(d: dict) -> list[str]:
    det = d.get("detail") or {}
    lat, vb = det.get("latency_ms") or {}, det.get("vs_baselines") or {}
    out = [f"- 실행: {d.get('stopped_by')} · {d.get('elapsed_sec')}초 · 기사 {d.get('drivers')}명 · 배정 라운드 {d.get('rounds'):,}회",
           f"- 매칭 성공 {d.get('matched'):,}/{d.get('orders'):,}건 ({d.get('match_rate_pct')}%) · 만료 {d.get('expired', 0)}건 · "
           f"배정 지연 p50 {lat.get('p50')}ms · p95 {lat.get('p95')}ms · p99 {lat.get('p99')}ms",
           f"- 공차 거리: 헝가리안 {vb.get('hungarian_avg_km')}km vs 최단거리 {vb.get('nearest_avg_km')}km "
           f"(선택지 있던 라운드 {vb.get('comparable_rounds')}/{vb.get('all_rounds')})"]
    if det.get("by_day"):
        out.append("- 날짜별 매칭: " + " · ".join(f"{k}일 {v['match_rate_pct']}%" for k, v in det["by_day"].items()))
    if det.get("by_hub"):
        out.append("- 거점별 매칭: " + " · ".join(f"{k} {v['match_rate_pct']}%" for k, v in
                                               sorted(det["by_hub"].items(), key=lambda kv: kv[1]["match_rate_pct"] or 0)))
    ie = d.get("input_error")
    if ie and ie.get("checked"):  # 신고 무게 ≠ 실제 무게일 때 물리 한계(허용하중 100%)를 넘는 비율
        err = ie.get("error_range_pct") or [-5, 10]
        margin = ie.get("top_load_margin")
        out.append(f"- 입력 오차 반영 실제 위험률 (신고 대비 실제 {err[0]:+g} ~ {err[1]:+g}%"
                   + (f" · 상부 허용하중 여유 {margin:.0%}" if margin is not None else "") + f"): "
                   f"**{ie.get('risk_rate_pct')}%** ({ie.get('any', 0):,}/{ie['checked']:,}건 · "
                   f"상부 허용하중 초과 {ie.get('top_load', 0):,} · 적재중량 초과 {ie.get('overweight', 0)} · "
                   f"축중량 {ie.get('axle', 0)} · 최대 초과 +{ie.get('top_load_worst_pct', 0)}%)"
                   + (f" · 품목 주문 유효 적재율 {ie['avg_item_util_pct']}%" if ie.get("avg_item_util_pct") else ""))
    return out


def seed_table(results: dict[int, dict]) -> list[str]:
    seeds = sorted(results)
    def row(label, f):
        return f"| {label} | " + " | ".join(f(results[s]) for s in seeds) + " |"
    bc = lambda d: d.get("bad_cargo") or {}
    lines = ["| 시드 | " + " | ".join(f"{s}{' (KPI)' if s == KPI_SEED else ''}" for s in seeds) + " |",
             "|---|" + "---:|" * len(seeds),
             row("주문 생성 시도", lambda d: f"{d.get('attempts', 0):,}"),
             row("공차 거리 단축률", lambda d: f"{d.get('deadhead_vs_nearest_pct')}%"),
             row("거절률", lambda d: f"{d.get('rejection_rate_pct')}%"),
             row("배차 지연 P95", lambda d: f"{d.get('dispatch_latency_p95_sec', 0):.2f}초"),
             row("매칭 성공률", lambda d: f"{d.get('match_rate_pct')}%"),
             row("위반 화물 차단", lambda d: f"{sum(bc(d).get('blocked', {}).values()):,}/{sum(bc(d).get('injected', {}).values()):,}"),
             row("정상 화물 오차단", lambda d: f"{bc(d).get('clean_blocked', 0)}/{bc(d).get('clean_checked', 0):,}")]
    dh = [results[s]["deadhead_vs_nearest_pct"] for s in seeds if results[s].get("deadhead_vs_nearest_pct") is not None]
    total = sum(results[s].get("attempts", 0) for s in seeds)
    if dh:
        lines.append("")
        lines.append(f"→ {len(seeds)}개 시드 합계 주문 {total:,}건 · 공차 거리 단축률 평균 {sum(dh) / len(dh):.1f}% "
                     f"(범위 {min(dh)} ~ {max(dh)}%) · 목표(10%) 이상 {sum(1 for x in dh if x >= 10)}/{len(dh)}개 시드")
    return lines


def check_docker() -> bool:
    """도커 데스크톱이 켜져 있고 앱 이미지가 빌드돼 있는지 — 아니면 할 일을 알려 주고 False."""
    try:
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            print("도커가 실행 중이 아닙니다. Docker Desktop 을 켠 뒤 다시 실행하세요.")
            return False
    except FileNotFoundError:
        print("docker 명령을 찾을 수 없습니다. Docker Desktop 을 설치 · 실행한 뒤 다시 실행하세요.")
        return False
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
        print(f"도커 이미지 {IMAGE} 가 없습니다. 저장소 최상위 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
        return False
    return True


LAST_TEXT: str | None = None   # 마지막으로 출력한 결과 표 (실행 파일 맨 아래 결과 블록에 저장)
RESULT_MARK = "# ===== 마지막 실행 결과 (▷ 실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) ====="


def save_result_block(script: str | Path, text: str) -> None:
    """실행 파일 맨 아래에 결과를 ''' 결과 ''' 문자열로 붙인다 (이전 결과 블록은 교체)."""
    path = Path(script).resolve()
    src = path.read_text(encoding="utf-8")
    code = src.split(RESULT_MARK)[0].rstrip()
    body = text.replace("\\", "/").replace("'''", "’’’").strip()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    path.write_text(f"{code}\n\n\n{RESULT_MARK}\n'''\n저장 {stamp}\n\n{body}\n'''\n", encoding="utf-8")
    print(f"결과를 이 파일 맨 아래에 저장: {path.relative_to(ROOT).as_posix()}")


def show(seeds: list[int] | None) -> int:
    global LAST_TEXT
    base = _load(RESULTS / "benchmark_large.json")
    if not base:
        print("결과가 없습니다. 먼저 backend/tests/stress_tests/1_kpi_8k.py 를 실행하세요.")
        return 1
    md = ["## KPI 목표치 측정 (매칭 · 적재)", "", *kpi_table(base), "", *detail_lines(base)]
    results = {KPI_SEED: base}
    for f in sorted(RUNS.glob("seed*.json")):
        s = int(f.stem[4:])
        if seeds and s not in seeds:
            continue
        d = _load(f)
        if d:
            results[s] = d
    if len(results) > 1:
        md += ["", "## 시드별 결과", "", *seed_table(results)]
    text = "\n".join(md)
    LAST_TEXT = text
    print("\n" + text + "\n")
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "summary.md").write_text(text + "\n", encoding="utf-8")
    print(f"표 저장: {(RUNS / 'summary.md').relative_to(ROOT).as_posix()} (보고서에 붙여 넣기용)")
    return 0


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KPI 목표치 측정 (매칭 · 적재) — 주문 8,000건 이상 스트레스 테스트")
    ap.add_argument("--seeds", type=int, nargs="+", default=[KPI_SEED], help="실행할 시드 (기본 7 = KPI 기준)")
    ap.add_argument("--days", type=int, default=8, help="시뮬레이션 날짜 수 (기본 8)")
    ap.add_argument("--orders", type=int, default=1001, help="하루 주문 수 (기본 1,001 → 8일이면 8,008건)")
    ap.add_argument("--drivers", type=int, default=400, help="가상 기사 수 (기본 400)")
    ap.add_argument("--parallel", type=int, default=4, help="동시에 돌릴 시드 수 (기본 4)")
    ap.add_argument("--show", action="store_true", help="실행 없이 마지막 결과만 표로 보기")
    ap.add_argument("--local", action="store_true", help="도커 대신 로컬 파이썬으로 실행")
    a = ap.parse_args(argv)
    if a.show:
        return show(a.seeds if a.seeds != [KPI_SEED] else None)
    if a.days * a.orders < 8000:
        print(f"참고: 주문 {a.days * a.orders:,}건 — 8,000건 이상이 되려면 --days · --orders 를 늘리세요.")
    if not a.local and not check_docker():
        return 1
    RUNS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    print(f"주문 {a.days * a.orders:,}건 (하루 {a.orders:,} × {a.days}일) · 기사 {a.drivers}명 · 시드 {a.seeds}")
    t0 = time.time()
    done: list[int] = []
    # KPI 기준 시드는 혼자 돌려야 배정 지연이 다른 실행 때문에 부풀지 않는다 → 먼저 단독, 나머지는 병렬
    if KPI_SEED in a.seeds:
        s, out, sec = run_one(KPI_SEED, a, parallel=False)
        if out:
            done.append(s)
            print(f"  [시드 {s}] 완료 {sec:.0f}초", flush=True)
    others = [s for s in a.seeds if s != KPI_SEED]
    if others:
        with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            for s, out, sec in ex.map(lambda s: run_one(s, a, parallel=len(others) > 1), others):
                if out:
                    done.append(s)
                    print(f"  [시드 {s}] 완료 {sec:.0f}초", flush=True)
    print(f"\n전체 {time.time() - t0:.0f}초 · 성공 {len(done)}/{len(a.seeds)}개 시드")
    show(sorted(done) if done else None)
    return 0 if len(done) == len(a.seeds) else 1


if __name__ == "__main__":
    sys.exit(main())
