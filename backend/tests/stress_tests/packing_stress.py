"""
적재 전용 대량 스트레스 테스트 — 매칭 없이 '규칙 + EP 적재 엔진 · 독립 검증기'만 수천 ~ 수만 건 돌린다.

각 케이스 (같은 화물을 상부 허용하중 여유값마다 다시 적재)
  - 정상 화물: 무작위 품목 1~4종 (팔레트 · 박스 · 드럼 · 장비 · 장척물 / 냉동 · 파손주의 · 적층 불가 · 상부 허용하중 ·
    세움 고정 · 다중 하차) → 차종 자동 추천 + 적재 → 등록 가능 여부 · 적재율 · 차종 · 처리 시간
      · 신고값 재검증: 독립 검증기로 다시 채점 → 위반 0건이어야 함 (KPI '과적 · 적재 오탐 0%')
      · 실제 무게 재검증: 품목마다 신고 대비 실제 무게 오차(--errors 범위별)를 주고 물리 한계
        (최대 적재중량 · 상부 허용하중 100% · 축중량)를 넘는지 → 현실 위험률
  - 위반 화물 (--bad-rate): 과적 · 편심 · 상부 하중 초과 · 적층 불가 위 적재 · 냉장 화물 상온차 · 적재함보다 큰 화물
    → 화주가 고른 차종으로 적재 → 등록 단계에서 막히는지 (차단률)

실행 (backend 폴더 · 보통은 tests/stress_tests/5_packing_stress.py 가 도커로 실행)
    python -m tests.stress_tests.packing_stress --cases 10000 --margins 0 0.05 0.1 0.15 --errors=-5:5,-5:10,-15:15 --bad-rate 0.15
결과: --out JSON (tests/stress_tests/_packing.py 가 표로 정리)
      10,000건 이상 · 시드 7 기준 실행이면 backend/src/data/benchmark_packing.json 사본도 저장 → 관제 GET /admin/kpi 의 적재 · 과적 행
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend 폴더
REPO = os.path.dirname(BACKEND)                                                         # 저장소 최상위 (simulator)
for _p in (BACKEND, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.config import settings  # noqa: E402
from src.engines.packer.engine import Options  # noqa: E402
from src.engines.packer.models import ItemSpec  # noqa: E402
from src.engines.packer.service import PackingError, plan  # noqa: E402
from src.engines.packer.trucks import TRUCKS  # noqa: E402
from src.engines.packer.validate import validate  # noqa: E402
from simulator import virtual_shipper as vs  # noqa: E402

BAD_KINDS = ["overload", "eccentric", "top_load", "nostack", "cold_mismatch", "oversize"]
BAD_LABEL = {"overload": "과적 (최대 적재량 110~120%)", "eccentric": "편심 (장비 1대 앞쪽 쏠림)",
             "top_load": "상부 허용하중 초과 (쌓을 수밖에 없는 물량)", "nostack": "적층 불가 품목 위 적재 (바닥 부족)",
             "cold_mismatch": "냉장 화물 → 상온 차량", "oversize": "적재함보다 큰 화물"}
MAX_BOXES = 200
WARM = [t for t in TRUCKS if not TRUCKS[t].refrigerated]
COLD = [t for t in TRUCKS if TRUCKS[t].refrigerated]


# ---------------------------------------------------------------- 정상 화물 생성
def _sku(rng: random.Random, kind: str, cold: bool, drop: int) -> dict:
    up, stack, frag, frozen = True, True, False, False
    if kind == "pallet":
        L, W = rng.choice([(1100, 1100), (1200, 1000), (1200, 800)])
        H, w, q = rng.randrange(900, 1701, 50), rng.uniform(250, 900), rng.randint(2, 16)
        stack = rng.random() < 0.75
        top = w * rng.uniform(0.6, 2.5)
        frag = rng.random() < 0.05
    elif kind == "carton":
        L, W, H = rng.randrange(300, 801, 50), rng.randrange(250, 601, 50), rng.randrange(200, 601, 50)
        w, q = rng.uniform(5, 35), rng.randint(10, 80)
        up, stack = rng.random() < 0.5, rng.random() < 0.9
        top = w * rng.uniform(2, 10)
        frag = rng.random() < 0.1
    elif kind == "drum":
        L = W = 600
        H, w, q = 900, rng.uniform(150, 250), rng.randint(4, 24)
        stack = rng.random() < 0.6
        top = w * rng.uniform(1, 3)
    elif kind == "machine":
        L, W, H = rng.randrange(800, 2001, 100), rng.randrange(700, 1601, 100), rng.randrange(800, 1801, 100)
        w, q = rng.uniform(400, 3000), rng.randint(1, 3)
        stack = rng.random() < 0.2
        top = rng.uniform(100, 400)
        frag = rng.random() < 0.2
    else:  # long — 장척물 (파이프 · 형강 묶음)
        L, W, H = rng.randrange(2000, 4001, 100), rng.randrange(300, 601, 50), rng.randrange(300, 501, 50)
        w, q = rng.uniform(50, 300), rng.randint(2, 10)
        up = False
        top = w * rng.uniform(2, 5)
    if cold:
        frozen = rng.random() < 0.4
    return dict(name=f"{kind}", length=L, width=W, height=H, weight=round(w, 1), qty=q, upright=up,
                stackable=stack, max_top_load=round(top) if stack else None, drop=drop, fragile=frag, frozen=frozen)


def clean_cargo(rng: random.Random) -> tuple[bool, list[dict]]:
    cold = rng.random() < 0.2
    kinds = ["pallet", "carton"] if cold else ["pallet", "carton", "drum", "machine", "long"]
    multi = rng.random() < 0.3
    items = [_sku(rng, rng.choice(kinds), cold, rng.randint(1, 3) if multi else 1) for _ in range(rng.randint(1, 4))]
    # 가장 큰 호환 차량의 70%(부피) · 80%(중량) 안쪽 · 상자 200개 이하로 수량 조정 → '실을 수 있는' 화물
    big = max((TRUCKS[t] for t in (COLD if cold else WARM)), key=lambda c: c.max_weight)
    vol = sum(i["length"] * i["width"] * i["height"] * i["qty"] for i in items)
    wt = sum(i["weight"] * i["qty"] for i in items)
    n = sum(i["qty"] for i in items)
    k = min(1.0, big.volume * 0.7 / vol, big.max_weight * 0.8 / wt, MAX_BOXES / n)
    if k < 1:
        for i in items:
            i["qty"] = max(1, int(i["qty"] * k))
    return cold, items


# ---------------------------------------------------------------- 위반 화물 생성
def bad_cargo(rng: random.Random, kind: str) -> tuple[str, bool, list[dict]]:
    """→ (화주가 고른 차종, 냉장 여부, 품목). 등록 단계에서 막혀야 정상."""
    if kind in ("overload", "eccentric"):
        _, truck, items = vs.bad_cargo(rng, kind)
        return truck, False, items
    if kind in ("top_load", "nostack"):
        truck = rng.choice(WARM)
        c = TRUCKS[truck]
        floor = (c.length // 1000) * (c.width // 1000)       # 1m × 1m 상자가 바닥에 놓이는 개수
        n = floor + rng.randint(1, max(1, floor))             # 바닥이 모자라 반드시 쌓아야 하는 물량 (2단 이내)
        w = min(60.0, c.max_weight * 0.5 / n)
        stack = kind == "top_load"
        return truck, False, [dict(name="허용하중 낮은 상자" if stack else "적층 불가 상자", length=1000, width=1000,
                                   height=500, weight=round(w, 1), qty=n, upright=True, stackable=stack,
                                   max_top_load=round(w * 0.3, 1) if stack else None, drop=1)]
    if kind == "cold_mismatch":
        truck = rng.choice(WARM)
        return truck, True, [dict(name="냉동 수산물", length=1100, width=1100, height=1200, weight=400,
                                  qty=rng.randint(1, 4), upright=True, stackable=True, max_top_load=800, drop=1,
                                  frozen=True)]
    truck = rng.choice(WARM)  # oversize — 어느 방향으로 돌려도 적재함에 안 들어가는 크기
    t = sorted((TRUCKS[truck].length, TRUCKS[truck].width, TRUCKS[truck].height))
    return truck, False, [dict(name="초대형 구조물", length=t[2] + 300, width=t[1] + 100, height=t[0] + 100,
                               weight=min(500, TRUCKS[truck].max_weight * 0.3), qty=1, upright=False,
                               stackable=False, max_top_load=None, drop=1)]


# ---------------------------------------------------------------- 케이스 1건 (워커 프로세스)
CFG: dict = {}


def _init(cfg: dict) -> None:
    CFG.update(cfg)


def _plan(items: list[dict], truck: str | None, cold: bool) -> tuple[dict | None, float, str | None]:
    t0 = time.perf_counter()
    try:
        res = plan([ItemSpec(**i) for i in items], truck_type=truck, cold_chain=cold, options=Options(record_steps=False))
        return res, (time.perf_counter() - t0) * 1000, None
    except PackingError as e:
        return None, (time.perf_counter() - t0) * 1000, str(e)


def _recheck(res: dict, cold: bool, errs: list[float]) -> dict:
    """신고 무게로 만든 계획을 실제 무게로 다시 채점 (물리 한계: 상부 허용하중 100%)."""
    c = TRUCKS[res["truck"]["name"]]
    rows = [dict(r, weight_kg=r["weight_kg"] * (1 + errs[r["sku"]])) for r in res["placements"]]
    o = res.get("options") or {}
    chk = {x["key"]: x for x in validate(c, rows, min_support=o.get("min_support", 0.8), lifo=o.get("lifo", True),
                                         cold_chain=cold, safety_margin=settings.safety_margin, top_load_margin=0.0)}
    actual = sum(r["weight_kg"] for r in rows)
    top = chk["stacking"]["status"] == "fail"
    return {"over": actual > c.max_weight + 1e-6, "top": top, "axle": chk["cog"]["status"] == "fail",
            "excess": round((chk["stacking"].get("worst_ratio", 1.0) - 1) * 100, 2) if top else 0.0}


def run_case(i: int) -> dict:
    rng = random.Random(CFG["seed"] * 1_000_003 + i)
    out: dict = {"i": i, "by_margin": {}}
    if rng.random() < CFG["bad_rate"]:
        kind = rng.choice(BAD_KINDS)
        truck, cold, items = bad_cargo(rng, kind)
        out.update(type="bad", kind=kind)
        for m in CFG["margins"]:
            settings.top_load_margin = m
            res, ms, err = _plan(items, truck, cold)
            reasons = [err] if err else vs.rejection_reasons(res)
            out["by_margin"][str(m)] = {"blocked": bool(reasons), "reasons": [r.split(" (")[0] for r in reasons]}
        return out

    cold, items = clean_cargo(rng)
    out.update(type="clean", cold=cold, skus=len(items), boxes=sum(x["qty"] for x in items))
    # 입력 오차는 여유값과 상관없이 같은 값 (공정 비교) — 오차 범위마다 품목별로 한 번 뽑음
    errs = {f"{lo:g}:{hi:g}": [rng.uniform(lo, hi) / 100 for _ in items] for lo, hi in CFG["errors"]}
    for m in CFG["margins"]:
        settings.top_load_margin = m
        res, ms, err = _plan(items, None, cold)
        if res is None:
            out["by_margin"][str(m)] = {"ok": False, "reasons": [err], "ms": round(ms, 1)}
            continue
        reasons = vs.rejection_reasons(res)
        r = {"ok": not reasons, "reasons": [x.split(" (")[0] for x in reasons], "ms": round(ms, 1),
             "truck": res["truck"]["name"], "vol": res["metrics"]["volume_util"], "wt": res["metrics"]["weight_util"]}
        if r["ok"]:
            c = TRUCKS[r["truck"]]
            o = res.get("options") or {}
            again = validate(c, res["placements"], min_support=o.get("min_support", 0.8), lifo=o.get("lifo", True),
                             cold_chain=cold, safety_margin=settings.safety_margin, top_load_margin=m)
            r["declared_fail"] = any(x["status"] == "fail" for x in again)
            r["real"] = {k: _recheck(res, cold, e) for k, e in errs.items()}
        out["by_margin"][str(m)] = r
    return out


# ---------------------------------------------------------------- 집계
def _bucket(ex: float) -> str:
    return "≤2%" if ex <= 2 else "2~5%" if ex <= 5 else "5~10%" if ex <= 10 else ">10%"


def _size(name: str) -> tuple:
    return TRUCKS[name].max_weight, TRUCKS[name].volume


def aggregate(results: list[dict], margins: list[float], errors: list[tuple[float, float]]) -> dict:
    clean = [r for r in results if r["type"] == "clean"]
    bad = [r for r in results if r["type"] == "bad"]
    base = str(margins[0])
    out = {}
    for m in map(str, margins):
        rows = [r["by_margin"][m] for r in clean]
        ok = [x for x in rows if x["ok"]]
        ms = sorted(x["ms"] for x in rows)
        fails = Counter(reason for x in rows if not x["ok"] for reason in x["reasons"])
        bigger = sum(1 for r in clean if r["by_margin"][m]["ok"] and r["by_margin"][base].get("ok")
                     and _size(r["by_margin"][m]["truck"]) > _size(r["by_margin"][base]["truck"]))
        lost = sum(1 for r in clean if not r["by_margin"][m]["ok"] and r["by_margin"][base].get("ok"))
        real = {}
        for lo, hi in errors:
            k = f"{lo:g}:{hi:g}"
            rr = [x["real"][k] for x in ok]
            top = [x for x in rr if x["top"]]
            real[k] = {"checked": len(rr), "any": sum(1 for x in rr if x["over"] or x["top"] or x["axle"]),
                       "top": len(top), "over": sum(x["over"] for x in rr), "axle": sum(x["axle"] for x in rr),
                       "worst": max((x["excess"] for x in top), default=0.0),
                       "buckets": dict(Counter(_bucket(x["excess"]) for x in top))}
            real[k]["rate"] = round(100 * real[k]["any"] / len(rr), 2) if rr else None
        bk = {}
        for kind in BAD_KINDS:
            xs = [r["by_margin"][m] for r in bad if r["kind"] == kind]
            bk[kind] = {"injected": len(xs), "blocked": sum(x["blocked"] for x in xs),
                        "reasons": dict(Counter(reason for x in xs for reason in x["reasons"]).most_common(3))}
        out[m] = {
            "clean": len(rows), "ok": len(ok), "fail": len(rows) - len(ok), "fail_reasons": dict(fails.most_common(5)),
            "declared_fail": sum(1 for x in ok if x.get("declared_fail")),
            "avg_vol_pct": round(100 * sum(x["vol"] for x in ok) / len(ok), 1) if ok else None,
            "avg_wt_pct": round(100 * sum(x["wt"] for x in ok) / len(ok), 1) if ok else None,
            "trucks": dict(Counter(x["truck"] for x in ok)),
            "bigger_truck_vs_base": bigger, "lost_vs_base": lost,
            "ms_avg": round(sum(ms) / len(ms), 1) if ms else None, "ms_p95": ms[int(len(ms) * 0.95)] if ms else None,
            "real": real, "bad": bk,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="적재 전용 대량 스트레스 테스트")
    ap.add_argument("--cases", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--margins", type=float, nargs="+", default=[0.0, 0.05, 0.10, 0.15])
    ap.add_argument("--errors", default="-5:5,-5:10,-15:15", help="신고 대비 실제 무게 오차 범위들 (%%) 예: --errors=-5:5,-5:10")
    ap.add_argument("--bad-rate", type=float, default=0.15)
    ap.add_argument("--workers", type=int, default=0, help="동시 프로세스 수 (0 = CPU 수)")
    ap.add_argument("--out", default="tests/stress_tests/results/packing/packing_stress.json")
    a = ap.parse_args()
    errors = [tuple(float(v) for v in e.split(":")) for e in a.errors.split(",") if e.strip()]
    margins = list(dict.fromkeys(round(m, 4) for m in a.margins))
    workers = a.workers or os.cpu_count() or 1
    cfg = {"seed": a.seed, "bad_rate": a.bad_rate, "margins": margins, "errors": errors}
    print(f"적재 스트레스: {a.cases:,}건 · 여유 {', '.join(f'{m:.0%}' for m in margins)} · "
          f"오차 {', '.join(f'{lo:+g}~{hi:+g}%' for lo, hi in errors)} · 위반 화물 {a.bad_rate:.0%} · "
          f"프로세스 {workers}개", flush=True)
    t0 = time.time()
    results, step = [], max(1, a.cases // 20)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(cfg,)) as ex:
        for n, r in enumerate(ex.map(run_case, range(a.cases), chunksize=8), 1):
            results.append(r)
            if n % step == 0 or n == a.cases:
                el = time.time() - t0
                print(f"  진행 {n:,}/{a.cases:,} ({100 * n / a.cases:.0f}%) · {el:.0f}초 · "
                      f"남은 시간 약 {el / n * (a.cases - n):.0f}초", flush=True)
    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "cases": a.cases, "seed": a.seed, "margins": margins,
            "errors": [list(e) for e in errors], "bad_rate": a.bad_rate, "workers": workers,
            "elapsed_sec": round(time.time() - t0, 1),
            "clean_cases": sum(1 for r in results if r["type"] == "clean"),
            "bad_cases": sum(1 for r in results if r["type"] == "bad"),
            "bad_labels": BAD_LABEL, "by_margin": aggregate(results, margins, errors)}
    path = os.path.join(BACKEND, a.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"완료: {data['elapsed_sec']}초 · 저장 {a.out}", flush=True)
    if a.cases >= 10000 and a.seed == 7:  # 기준 실행만 관제 GET /admin/kpi 사본으로 (도커 이미지에 포함되는 위치)
        app = os.path.join(BACKEND, "src", "data", "benchmark_packing.json")
        with open(app, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("저장: backend/src/data/benchmark_packing.json (관제 /admin/kpi 적재 · 과적 기준)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
