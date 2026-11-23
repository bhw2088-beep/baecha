"""
Phase 6: 마이크로 배치 매칭 스트레스 테스트 + GPS 디바운싱 오탐 검증 (서버 없이, 메모리 저장소)

시나리오
  1) 가상 기사 군집: 물류 거점 주변 가우스 분포 위치 · 표준 톤수(1 · 2.5 · 5 · 11 · 25톤) · 사업용 번호판
  2) 주문: 하루 24시간을 압축, 시간대별 포아송 콜(러시아워 폭증) · 거점 클러스터링 (simulator/virtual_shipper.py)
  3) 마이크로 배치: 한 '틱'(3~5초 창)마다 쌓인 주문을 한 번에 헝가리안 배정 (Redis 분산 락 경로 그대로)
  4) 기사 응답: 로지스틱 확률 규칙(거리 · 적재율 · 운임 · 권역 선호 · 평점 · 러시아워)으로 수락/거절
  5) 적재 오탐: 배차된 화물마다 배정 차량 기준 적재 계획을 다시 검증 → 안전율 · 축중량 · 중량 위반 0건인지
     + 위반 화물 주입: 주문의 12% 를 과적(최대 적재량 110~120%) · 편심(앞쪽 쏠림) 화물로 → 등록 단계에서 모두 차단되는지,
       정상 샘플 화물이 잘못 차단(오차단)되지 않는지
     + 입력 오차: 화주가 신고한 무게와 실제 무게가 품목마다 −5 ~ +10% 다르다고 보고(--weight-error),
       신고값으로 배차한 화물을 '실제 무게'로 다시 검증 → 현실 과적 위험률 · 안전율(5%)이 흡수한 건수
  6) GPS: 정상 지터 · 단발 튐(GPS 스파이크) · 고의 이탈(연속 4회)을 섞어 핑 → 오탐 0건, 이탈은 정확히 3번째에 경고

  7) KPI 목표 판정 (engines/kpi.py): 배차 p95 < 3초 · 거절률 < 15~20% · 단순 최단거리 대비 공차 거리 단축 ·
     적재 오탐 0% · GPS 오알람 < 5% — GET /admin/kpi 와 같은 잣대

실행: 수정본 폴더에서
    python tests/stress/stress_test.py                    # 기본 (주문 120 · 기사 45)
    python tests/stress/stress_test.py --quick            # CI 용 (주문 30 · 기사 18)
    python tests/stress/stress_test.py --out docs/설명/benchmark_metrics.json
    # 대규모 단발 배치 (한 줄): 8일 × 하루 1,001건 = 8,008건 · 기사 400명 · 30분 시간 제한
    #   → 관제 KPI(매칭 · 적재)의 측정 기준 (backend/data/benchmark_large.json 사본을 /admin/kpi 가 읽음)
    python tests/stress/stress_test.py --preset 8k          # 결과: tests/stress/stress_8k/results/benchmark_large.json · large_run/*.csv
    # 직접 지정: 5일 × 하루 1,000건 · 기사 300명 · 8건씩 배정 · 5,000건이나 15분이 되면 멈추고 집계
    python tests/stress/stress_test.py --days 5 --orders 1000 --drivers 300 --batch 8 --target 5000 --max-minutes 15 \
        --expire-hours 3 --detail --out tests/stress/stress_8k/results/benchmark_large.json --csv-dir tests/stress/stress_8k/results/large_run
      → backend/data/benchmark_metrics.json 에도 같은 내용을 저장 (GET /admin/kpi 가 GPS 오알람률을 읽음)
결과: '결과: N개 통과 · 0개 실패' + 메트릭 JSON
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta

from _common import check, finish, section

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 수정본
sys.path.insert(0, ROOT)

from backend.core.database import Store  # noqa: E402
from backend.engines import compliance_engine as comp  # noqa: E402
from backend.engines import kpi  # noqa: E402
from backend.engines import tracking_engine as te  # noqa: E402
from backend.engines.matcher import ml_ranker  # noqa: E402
from backend.engines.packer.samples import SAMPLES  # noqa: E402
from backend.engines.matcher.spatial import along  # noqa: E402
from backend.services import billing_service as pricing  # noqa: E402
from simulator import virtual_shipper as vs  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--orders", type=int, default=120)
ap.add_argument("--drivers", type=int, default=45)
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--quick", action="store_true")
ap.add_argument("--out", default=None)
ap.add_argument("--days", type=int, default=1, help="하루(24시간 포아송)를 며칠 반복할지 · --orders 는 하루 물량")
ap.add_argument("--batch", type=int, default=4, help="배정 라운드 한 번에 묶는 주문 수 (마이크로 배치 창)")
ap.add_argument("--target", type=int, default=None, help="주문 생성 시도가 이 건수가 되면 멈추고 집계 (건수 제한)")
ap.add_argument("--max-minutes", type=float, default=None, help="이 시간이 지나면 멈추고 그때까지로 집계 (시간 제한)")
ap.add_argument("--expire-hours", type=float, default=None,
                help="이 시간(시뮬레이션) 동안 배차 안 된 주문은 만료로 빼서 대기열이 끝없이 커지지 않게 (대규모용)")
ap.add_argument("--detail", action="store_true", help="시간대 · 거점 · 차종 · 분포 등 상세 통계를 결과에 포함")
ap.add_argument("--csv-dir", default=None, help="주문별 · 라운드별 CSV 저장 폴더")
ap.add_argument("--weight-error", type=float, nargs=2, default=[-5.0, 10.0], metavar=("MIN%", "MAX%"),
                help="화주 신고 무게 대비 실제 무게 오차 범위 (품목마다 균등분포, 기본 −5 ~ +10%%)")
ap.add_argument("--preset", choices=["5k", "8k"], default=None,
                help="대규모 단발 배치 프리셋 (아래 PRESETS 값으로 옵션을 채운다)")
args = ap.parse_args()
# 대규모 프리셋: 하루 물량은 현실적으로(1,000건) 두고 날짜를 늘려 총량을 채운다 · 기사는 하차지에서 다음 배차로 이어짐
PRESETS = {
    "5k": dict(days=5, orders=1000, drivers=300, batch=8, target=5000, max_minutes=20, expire_hours=3),
    "8k": dict(days=8, orders=1001, drivers=400, batch=8, target=8008, max_minutes=30, expire_hours=3),
}
if args.preset:
    for k, v in PRESETS[args.preset].items():
        setattr(args, k, v)
    args.detail = True
    args.out = args.out or "tests/stress/stress_8k/results/benchmark_large.json"
    args.csv_dir = args.csv_dir or "tests/stress/stress_8k/results/large_run"
if args.quick:
    args.orders, args.drivers = 30, 18
rng = random.Random(args.seed)
# 입력 오차는 별도 난수로 만든다 → 기존 시뮬레이션(주문 · 배차 · 수락)의 난수 흐름과 결과가 그대로 유지됨
err_rng = random.Random(args.seed + 1000)

# ------------------------------------------------------------------ 시뮬레이션 시계
# 러시아워 · 모의 기상 · 시간대별 수요가 '지금 시각'을 읽기 때문에, 실제 시계를 쓰면 같은 시드라도
# 실행 시각마다 결과가 달라진다. → 고정된 하루(2026-11-16)를 0시부터 24시까지 진행시키는 시계로 바꾼다.
SIM_DAY = datetime(2026, 11, 16)
_sim_now = [SIM_DAY]


class SimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _sim_now[0]


def set_sim_clock(hour: int, minute: int = 0, day: int = 0):
    _sim_now[0] = SIM_DAY.replace(hour=hour, minute=minute) + timedelta(days=day)


for _name, _mod in list(sys.modules.items()):
    if _name.startswith(("backend.services", "backend.engines", "backend.core", "simulator"))             and getattr(_mod, "datetime", None) is datetime:
        _mod.datetime = SimDateTime

# 적재 계획 캐시: 가상 화물은 샘플의 반복이라 같은 품목 · 같은 차종 · 같은 옵션이면 결과가 같다 → 한 번만 계산
from backend.engines.packer import service as _packer_service  # noqa: E402
_plan_original = _packer_service.plan
_plan_cache: dict[str, dict] = {}


def _cached_plan(items, *, truck_type, cold_chain, options):
    key = json.dumps([[vars(i) for i in items], truck_type, cold_chain, vars(options)], sort_keys=True, default=str)
    if key not in _plan_cache:
        _plan_cache[key] = _plan_original(items, truck_type=truck_type, cold_chain=cold_chain, options=options)
    return copy.deepcopy(_plan_cache[key])


_packer_service.plan = _cached_plan

store = Store()
store._run_matching = lambda *a, **k: None  # 주문 등록 때 바로 매칭하지 않고 틱마다 묶어서 (마이크로 배치)

# ------------------------------------------------------------------ 1) 가상 기사 군집
section(f"가상 기사 {args.drivers}명 · 주문 {args.orders}건 (시드 {args.seed})")
TRUCK_MIX = [("1톤 카고", 0.18), ("2.5톤 카고", 0.14), ("5톤 윙바디", 0.2), ("5톤 냉동탑차", 0.1),
             ("11톤 탑차", 0.18), ("11톤 냉동탑차", 0.12), ("25톤 무진동", 0.08)]
LETTERS = "바사아자"
for i in range(args.drivers):
    hub = vs.pick_hub(rng)
    loc = vs.nearest_region(vs.gaussian_point(hub, rng))
    truck = rng.choices([t for t, _ in TRUCK_MIX], weights=[w for _, w in TRUCK_MIX])[0]
    did = f"V-{i:03d}"
    plate = f"경기 {rng.randint(70, 99)}{rng.choice(LETTERS)} {rng.randint(1000, 9999)}"
    store.drivers[did] = {"id": did, "name": f"가상기사{i:03d}", "phone": "-", "plate": plate, "truck_type": truck,
                          "location": loc, "month_runs": rng.randint(5, 40),
                          "plate_type": comp.classify_plate(plate)["plate_type"], "tonnage": comp.tonnage(truck)}
check("가상 기사 전원 사업용 노란 번호판", all(d["plate_type"] == "COMMERCIAL_YELLOW"
                                        for d in store.drivers.values() if d["id"].startswith("V-")))

# ------------------------------------------------------------------ 2) 포아송 주문 타임라인
def day_calls() -> list[int]:
    calls = vs.hourly_calls(rng)
    total_calls = sum(calls) or 1
    return [max(0, round(c * args.orders / total_calls)) for c in calls]


days_per_hour = [day_calls() for _ in range(args.days)]
per_hour = days_per_hour[0]
rush = per_hour[8] + per_hour[18]
check("러시아워(08 · 18시) 콜이 새벽(02~04시)보다 많음", rush > sum(per_hour[2:5]), f"러시아워 {rush} · 새벽 {sum(per_hour[2:5])}")

samples = list(SAMPLES.values())


bad = {"injected": {"overload": 0, "eccentric": 0}, "blocked": {"overload": 0, "eccentric": 0}, "missed": [],
       "reasons": {}, "clean_checked": 0, "clean_blocked": 0}
meta: dict[str, dict] = {}            # 주문 id → 날짜 · 시간대 · 거점 · 요청 차종 · 품목 유무
day_now = [0]


def new_order(h: int, k: int) -> str | None:
    from backend.engines.packer.engine import Options
    from backend.engines.packer.models import ItemSpec
    from backend.engines.packer.service import plan
    route = vs.make_route(rng)
    if rng.random() < vs.BAD_CARGO_RATE:
        # 위반 화물 주입: 화주가 고른 차종으로 서버 적재 검증(plan) → 사유가 있으면 등록 거부(차단)
        kind, bad_truck, items = vs.bad_cargo(rng)
        res = plan([ItemSpec(**i) for i in items], truck_type=bad_truck, cold_chain=False,
                   options=Options(record_steps=False))
        bad["injected"][kind] += 1
        reasons = vs.rejection_reasons(res)
        if reasons:
            bad["blocked"][kind] += 1
            for r in reasons:
                key = r.split(" (")[0]
                bad["reasons"][key] = bad["reasons"].get(key, 0) + 1
            return None
        bad["missed"].append(kind)  # 놓침: 규칙을 어긴 화물이 그대로 등록됨
    use_items = rng.random() < 0.35
    if use_items:
        smp = rng.choice(samples)
        res = plan([ItemSpec(**i) for i in smp["items"]], truck_type=None, cold_chain=smp["cold_chain"],
                   options=Options(record_steps=False))
        bad["clean_checked"] += 1
        if vs.rejection_reasons(res):
            bad["clean_blocked"] += 1  # 오차단: 정상 화물인데 적재 규칙이 막음
        truck, cold = res["truck"]["name"], smp["cold_chain"]
    else:
        truck = rng.choice([t for t, _ in TRUCK_MIX])
        cold, res, smp = truck in pricing.COLD_TYPES, None, None
    est = pricing.estimate(truck, route["origin"], route["destination"], cold_chain=cold)
    price = int(est["recommended"] * rng.uniform(0.9, 1.12) / 5000) * 5000
    o = store.create_order("S-001", {"title": f"[스트레스 {day_now[0] + 1}일 {h:02d}시] {k}", "truck_type": truck,
                                     "origin": route["origin"], "destination": route["destination"],
                                     "price": price, "cold_chain": cold, "urgent": rng.random() < 0.15},
                           packing=res if res and not res["unplaced"] else None,
                           items=smp["items"] if smp else None)
    meta[o["id"]] = {"day": day_now[0] + 1, "hour": h, "hub_from": route["hub_from"], "hub_to": route["hub_to"],
                     "truck_requested": truck, "with_items": bool(smp)}
    if smp:  # 품목별 실제 무게 = 신고 무게 × (1 + 오차)
        lo, hi = args.weight_error
        meta[o["id"]]["weight_err"] = [round(err_rng.uniform(lo, hi) / 100, 4) for _ in smp["items"]]
    return o["id"]


# ------------------------------------------------------------------ 3~4) 마이크로 배치 + 로지스틱 응답
section("마이크로 배치 헝가리안 + 로지스틱 수락/거절")
offers = accepted = declined = 0
t_start = time.perf_counter()
deadline = t_start + args.max_minutes * 60 if args.max_minutes else None
tick_latency = []
attempts = 0
stopped_by = "완료 (모든 날짜 · 시간대)"
round_rows: list[dict] = []           # 배정 라운드별 기록 (매칭 로그는 최근 500개만 남으므로 따로 모은다)
hourly: dict[tuple[int, int], dict] = {}   # (날짜, 시간대) → 제안 · 수락 · 거절 · 지연
expired: dict[str, dict] = {}          # 만료된 주문 (--expire-hours)
seen_round = [None]


def _hourly(day: int, h: int) -> dict:
    return hourly.setdefault((day, h), {"offers": 0, "accepted": 0, "declined": 0, "latency": []})


for day, per_hour_d in enumerate(days_per_hour):
    day_now[0] = day
    for h, n in enumerate(per_hour_d):
        for k in range(n):
            if args.target and attempts >= args.target:
                stopped_by = f"건수 제한 {args.target:,}건 도달"
                break
            if deadline and time.perf_counter() > deadline:
                stopped_by = f"시간 제한 {args.max_minutes:g}분 도달"
                break
            set_sim_clock(h, k * 60 // max(n, 1), day)   # 그 시간대 안에서 주문 순서대로 시각을 나눈다
            new_order(h, k)
            attempts += 1
            if (k + 1) % args.batch == 0 or k == n - 1:   # 주문 batch 건(≈3~5초 창)마다 한 번 배정 라운드
                t0 = time.perf_counter()
                store._matching_round(SimDateTime.now())
                ms = (time.perf_counter() - t0) * 1000
                tick_latency.append(ms)
                hr = _hourly(day + 1, h)
                hr["latency"].append(ms)
                if store.metrics_log and store.metrics_log[-1]["id"] != seen_round[0]:
                    e = store.metrics_log[-1]
                    seen_round[0] = e["id"]
                    round_rows.append({**{k2: v for k2, v in e.items() if k2 != "at"}, "day": day + 1, "hour": h,
                                       "round_ms": round(ms, 2)})
                rates = store._accept_rates()
                for o in [o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"]]:
                    did = o["offer"]["driver_id"]
                    feats = store._ml_features(o, did, o["offer"]["pickup_km"], o["offer"]["util"] or 0.5, rates)
                    offers += 1
                    hr["offers"] += 1
                    if rng.random() < ml_ranker._true_accept_prob(feats):   # 로지스틱 확률 규칙
                        store.accept_offer(did, o["id"])
                        accepted += 1
                        hr["accepted"] += 1
                    else:
                        o["declined"].append(did)
                        o["offer"] = None
                        store._log(o, "declined", did, SimDateTime.now(), note="스트레스 테스트 로지스틱 거절")
                        declined += 1
                        hr["declined"] += 1
                # 운행 완료 처리 → 기사를 하차지 공차로 되돌린다 (공급 순환)
                for o in list(store.orders.values()):
                    if o["status"] == "ASSIGNED" and o["title"].startswith("[스트레스") and rng.random() < 0.6:
                        store.start_run(o["driver_id"], o["id"])
                        store.complete_run(o["driver_id"], o["id"], "인수자", photo={"filename": "pod.jpg", "size": 1024, "sha256": "ab" * 32})
                # 대규모용: 오래 배차 안 된 주문은 만료 (대기열이 끝없이 커져 라운드가 느려지는 것 방지)
                if args.expire_hours:
                    cut = SimDateTime.now() - timedelta(hours=args.expire_hours)
                    for oid in [oid for oid, o in store.orders.items()
                                if oid in meta and o["status"] == "MATCHING" and not o["offer"] and o["created_at"] < cut]:
                        expired[oid] = store.orders.pop(oid)
        else:
            continue
        break
    else:
        if args.days > 1:  # 대규모 실행 진행 표시 (날짜마다 한 줄)
            done = sum(1 for oid in meta if meta[oid]["day"] == day + 1)
            print(f"  {day + 1}일차 완료 · 누적 주문 시도 {attempts:,}건 (이날 등록 {done:,}) · "
                  f"경과 {time.perf_counter() - t_start:.0f}초", flush=True)
        continue
    break
elapsed = time.perf_counter() - t_start
print(f"  실행: {stopped_by} · 주문 생성 시도 {attempts:,}건 · {elapsed:.0f}초 · 적재 계획 캐시 {len(_plan_cache)}종")
m = store.matching_metrics()
# 서버 매칭 로그는 최근 500 라운드만 남는다 → 공차 거리 비교는 이번 실행에서 모은 전체 라운드로 다시 계산
_cmp = [r for r in round_rows if r["assigned"] and r.get("has_choice", r.get("drivers", 0) >= 2)]
_nr = [r for r in _cmp if r.get("deadhead_vs_nearest_pct") is not None]
_h_km, _n_km = sum(r["hungarian_avg_km"] * r["assigned"] for r in _nr), sum(r["nearest_avg_km"] * r["nearest_assigned"] for r in _nr)
_h_n, _n_n = sum(r["assigned"] for r in _nr), sum(r["nearest_assigned"] for r in _nr)
_red = [r["deadhead_reduction_pct"] for r in _cmp if r["deadhead_reduction_pct"] is not None]
m = {**m, "deadhead_vs_nearest_pct": round((_n_km / _n_n - _h_km / _h_n) / (_n_km / _n_n) * 100, 1)
     if _h_n and _n_n and _n_km else None,
     "empty_deadhead_km_reduction_pct": round(sum(_red) / len(_red), 1) if _red else None}
lat = sorted(tick_latency)
p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else 0
stress_orders = [o for o in store.orders.values() if o["title"].startswith("[스트레스")] + list(expired.values())
matched = [o for o in stress_orders if o["driver_id"]]
check("배정 라운드 실행", len(tick_latency) > 0, f"{len(tick_latency)}회 · 총 {elapsed:.1f}초")
check("배차 지연 p95 < 3초", p95 / 1000 < 3.0, f"p95 {p95:.0f}ms")
check("매칭 성공 화물 있음", len(matched) > 0, f"{len(matched)}/{len(stress_orders)}건")
check("임계값 0.75 미만 후보는 자동 제안 안 됨",
      all((o["offer"] or {}).get("ml_prob", 1) >= 0.75 for o in store.orders.values() if o["offer"]))
rej_rate = round(100 * declined / offers, 1) if offers else None

# ------------------------------------------------------------------ 5) 적재 오탐 · 과적
section("물리적 적재 오탐 · 과적 (배정 차량 기준 재검증)")
violations, checked = 0, 0
for o in matched:
    if not o["packing"]:
        continue
    plan_ = store._plan_for_truck(o, store.drivers[o["driver_id"]]["truck_type"])
    checked += 1
    if plan_.get("unplaced") or any(c["status"] == "fail" for c in plan_["constraints"]):
        violations += 1
check("배정된 화물 과적 · 안전율 · 축중량 위반 0건", violations == 0, f"검증 {checked}건 · 위반 {violations}건")
inj, blk = sum(bad["injected"].values()), sum(bad["blocked"].values())
check("주입한 위반 화물(과적 · 편심) 모두 등록 단계에서 차단", inj > 0 and not bad["missed"],
      f"주입 {inj}건(과적 {bad['injected']['overload']} · 편심 {bad['injected']['eccentric']}) → 차단 {blk}건 · "
      f"놓침 {len(bad['missed'])}건")
print("    차단 사유: " + " · ".join(f"{k} {v}" for k, v in sorted(bad["reasons"].items(), key=lambda kv: -kv[1])))
check("정상 샘플 화물 오차단 0건", bad["clean_blocked"] == 0,
      f"정상 화물 {bad['clean_checked']}건 중 오차단 {bad['clean_blocked']}건")

# ---- 입력 오차 반영: 신고 무게로 배차된 화물을 '실제 무게'로 다시 검증 (현실에서 과적이었을 비율)
from backend.config import settings as _settings  # noqa: E402
from backend.engines.packer.trucks import TRUCKS as _TRUCKS  # noqa: E402
from backend.engines.packer.validate import validate as _validate  # noqa: E402
real = {"checked": 0, "overweight": 0, "top_load": 0, "axle": 0, "any": 0, "absorbed": 0, "over_declared": 0,
        "max_util_pct": 0.0, "top_load_excess": {"≤2%": 0, "2~5%": 0, "5~10%": 0, ">10%": 0}, "top_load_worst_pct": 0.0,
        "top_load_margin": _settings.top_load_margin}
_item_utils = []  # 품목(3D 적재) 주문의 배정 차량 기준 유효 적재율
_limit = 1 - _settings.safety_margin
for o in matched:
    errs = meta.get(o["id"], {}).get("weight_err")
    if not o["packing"] or not errs:
        continue
    truck_d = store.drivers[o["driver_id"]]["truck_type"]
    plan_ = store._plan_for_truck(o, truck_d)
    c = _TRUCKS[truck_d]
    rows = [dict(r, weight_kg=r["weight_kg"] * (1 + errs[r["sku"]])) for r in plan_["placements"]]
    declared = sum(r["weight_kg"] for r in plan_["placements"]) / c.max_weight
    actual = sum(r["weight_kg"] for r in rows) / c.max_weight
    opts = plan_.get("options") or {}
    chk = {x["key"]: x for x in _validate(c, rows, min_support=opts.get("min_support", 0.8), lifo=opts.get("lifo", True),
                                         cold_chain=o["cold_chain"], safety_margin=_settings.safety_margin,
                                         top_load_margin=0.0)}  # 물리 한계 그대로
    over, top, axle = actual > 1.0 + 1e-9, chk["stacking"]["status"] == "fail", chk["cog"]["status"] == "fail"
    real["checked"] += 1
    real["overweight"] += over
    real["top_load"] += top
    real["axle"] += axle
    real["any"] += over or top or axle
    real["over_declared"] += actual > declared + 1e-9
    real["absorbed"] += declared <= _limit + 1e-9 < actual <= 1.0 + 1e-9   # 안전율 구간(95~100%)이 오차를 흡수
    real["max_util_pct"] = max(real["max_util_pct"], round(actual * 100, 1))
    if (o.get("match") or {}).get("util") is not None:
        _item_utils.append(o["match"]["util"])
    if top:  # 상부 허용하중을 얼마나 넘었나 (가장 많이 넘은 상자 기준)
        ex = (chk["stacking"].get("worst_ratio", 1.0) - 1) * 100
        real["top_load_excess"]["≤2%" if ex <= 2 else "2~5%" if ex <= 5 else "5~10%" if ex <= 10 else ">10%"] += 1
        real["top_load_worst_pct"] = max(real["top_load_worst_pct"], round(ex, 1))
real["risk_rate_pct"] = round(100 * real["any"] / real["checked"], 2) if real["checked"] else None
real["avg_item_util_pct"] = round(100 * sum(_item_utils) / len(_item_utils), 1) if _item_utils else None
real["error_range_pct"] = list(args.weight_error)
section(f"입력 오차 반영 (신고 무게 대비 실제 {args.weight_error[0]:+g} ~ {args.weight_error[1]:+g}%)")
print(f"  배차 {real['checked']:,}건을 실제 무게로 재검증 → 물리적 위반 {real['any']}건 ({real['risk_rate_pct']}%) "
      f"[적재중량 초과 {real['overweight']} · 상부 허용하중 초과 {real['top_load']} · 축중량 {real['axle']}] · "
      f"실제가 신고보다 무거움 {real['over_declared']:,}건 · 총중량 안전율 5% 가 흡수 {real['absorbed']}건 · "
      f"최대 실제 중량 사용률 {real['max_util_pct']}%")
print("  상부 허용하중 초과 정도: " + " · ".join(f"{k} {v}건" for k, v in real["top_load_excess"].items())
      + f" (최대 +{real['top_load_worst_pct']}%)")

# ------------------------------------------------------------------ 6) GPS 디바운싱 오탐 검증
section("GPS 핑: 지터 · 단발 튐 · 고의 이탈 (디바운싱 3회)")
seed_runs = [o for o in store.orders.values() if o["status"] == "IN_TRANSIT" and not o["title"].startswith("[스트레스")]
false_alarms = detected = 0
alert_at = []
spikes = 0
for idx, o in enumerate(seed_runs[:8]):
    path = store._path(o)
    store.tracking.pop(o["id"], None)
    base = o["progress"] / 100
    deviate = idx % 2 == 0
    for i in range(24):
        ratio = min(0.97, base + i * 0.004)
        p, _ = along(path, ratio)
        if deviate and 10 <= i < 14:
            p = te.offset_point(path, ratio, 2.0)            # 고의 이탈 2km × 4회
        elif not deviate and i in (6, 15):
            p = te.offset_point(path, ratio, 3.0)            # GPS 튐 (단발)
            spikes += 1
        p = te.jitter(p, rng, 8.0)
        r = store.ingest_ping({"order_id": o["id"], "driver_id": o["driver_id"], "lat": p[0], "lon": p[1],
                               "speed": 80, "source": "stress"})
        if r.get("event") == "ALERT":
            alert_at.append(i - 10 + 1 if deviate else None)
    st = store.tracking[o["id"]]
    if deviate:
        detected += 1 if st["alerts"] == 1 else 0
    else:
        false_alarms += st["alerts"]
dev_runs = len(seed_runs[:8][::2])
check("GPS 튐(단발)으로 인한 오탐 0건", false_alarms == 0, f"스파이크 {spikes}회 · 오탐 {false_alarms}건")
check("고의 이탈은 모두 감지", detected == dev_runs, f"{detected}/{dev_runs}건")
check("이탈 경고는 정확히 연속 3번째 핑에서", alert_at and all(a == 3 for a in alert_at), f"{alert_at}")
o = seed_runs[0]
before = sum(1 for a in store.anomalies.values() if a["type"] == "SUSPICIOUS_REOUTSOURCING")
r = store.ingest_ping({"order_id": o["id"], "driver_id": "D-999", "lat": 37.0, "lon": 127.0, "speed": 70})
after = sum(1 for a in store.anomalies.values() if a["type"] == "SUSPICIOUS_REOUTSOURCING")
check("배정 기사가 아닌 단말 핑 → 거절 + 재위탁 의심 기록", not r["applied"] and after == before + 1)

# ------------------------------------------------------------------ 7) KPI 목표 판정 (GET /admin/kpi 와 같은 잣대)
section("KPI 목표 판정 (engines/kpi.py)")
normal_runs = len(seed_runs[:8]) - dev_runs
gps_fp = round(100 * false_alarms / normal_runs, 1) if normal_runs else None
kpi_rows = [
    kpi.evaluate("dispatch_p95_sec", round(p95 / 1000, 4), source="스트레스 테스트", basis=f"라운드 {len(tick_latency)}회"),
    kpi.evaluate("rejection_rate_pct", rej_rate, source="스트레스 테스트", basis=f"제안 {offers}건 · 거절 {declined}건"),
    kpi.evaluate("deadhead_vs_nearest_pct", m["deadhead_vs_nearest_pct"], source="스트레스 테스트",
                 basis="같은 후보 안에서 등록 순서 · 최단거리 배정과 비교"),
    kpi.evaluate("packing_violation_pct", round(100 * violations / checked, 2) if checked else None,
                 source="스트레스 테스트",
                 basis=f"배차 {checked}건 재검증 · 위반 {violations}건 · 위반 화물 주입 {inj}건 → 차단 {blk}건"),
    kpi.evaluate("gps_false_alarm_pct", gps_fp, source="스트레스 테스트",
                 basis=f"정상 운행 {normal_runs}회 · 단발 튐 {spikes}회 · 오알람 {false_alarms}건"),
]
for r in kpi_rows:
    print(f"  [{r['status_label']}] {r['name']}: {r['display']} (목표 {r['target']}) — {r['basis']}")
check("배차 지연 p95 KPI 달성 (< 3초)", kpi_rows[0]["status"] == "pass", kpi_rows[0]["display"])
check("적재 오탐 KPI 달성 (0%)", kpi_rows[3]["status"] == "pass", kpi_rows[3]["display"])
check("GPS 오알람 KPI 달성 (< 5%)", kpi_rows[4]["status"] == "pass", kpi_rows[4]["display"])

metrics = {
    "generated_at": datetime.now().isoformat(timespec="seconds"), "seed": args.seed,
    "drivers": args.drivers, "orders": len(stress_orders), "rounds": len(tick_latency),
    "orders_per_hour": per_hour,
    "matched": len(matched), "match_rate_pct": round(100 * len(matched) / max(len(stress_orders), 1), 1),
    "offers": offers, "accepted": accepted, "declined": declined, "rejection_rate_pct": rej_rate,
    "dispatch_latency_p95_ms": round(p95, 1), "dispatch_latency_p95_sec": round(p95 / 1000, 4),
    "dispatch_latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
    "empty_deadhead_km_reduction_pct": m["empty_deadhead_km_reduction_pct"],
    "deadhead_vs_nearest_pct": m["deadhead_vs_nearest_pct"],
    "threshold_filtered_pairs": m["threshold_filtered"], "ml_threshold": m["ml_threshold"],
    "packing_checked": checked, "packing_violations": violations,
    "input_error": real,
    "bad_cargo": {"rate": vs.BAD_CARGO_RATE, "injected": bad["injected"], "blocked": bad["blocked"],
                  "missed": len(bad["missed"]), "block_rate_pct": round(100 * blk / inj, 1) if inj else None,
                  "reasons": bad["reasons"], "clean_checked": bad["clean_checked"],
                  "clean_blocked": bad["clean_blocked"]},
    "overload_rate_pct": round(100 * violations / checked, 2) if checked else 0.0,
    "gps": {"runs": len(seed_runs[:8]), "spikes": spikes, "false_alarms": false_alarms, "deviation_runs": dev_runs,
            "detected": detected, "alert_ping_index": alert_at, "engine": "Shapely" if te.HAS_SHAPELY else "python",
            "false_alarm_rate_pct": gps_fp},
    "kpi": [{k: r[k] for k in ("key", "name", "target", "value", "display", "status", "basis")} for r in kpi_rows],
    "kpi_summary": kpi.summary(kpi_rows),
    "elapsed_sec": round(elapsed, 1),
}
metrics.update({"days": args.days, "batch": args.batch, "attempts": attempts, "stopped_by": stopped_by,
                "expired": len(expired), "orders_per_day": args.orders})


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def _pctl(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 1) if xs else None


def _fail_kind(o) -> str:
    if o["id"] in expired:
        return "만료 (대기 시간 초과)"
    text = " ".join(o.get("match_fail") or [])
    for key, label in (("전량 적재", "적재 가능 차량 없음"), ("운행 중", "적재 가능 차량 모두 운행 · 대기 중"),
                       ("반경", "반경 안 공차 없음"), ("임계값", "AI 수락 예측 임계값 차단"), ("거절", "거절 · 무응답")):
        if key in text:
            return label
    return "배정 대기 중" if o.get("match_wait") or not text else "기타"


order_rows = []
for o in stress_orders:
    mt = meta.get(o["id"], {})
    mm = o.get("match") or {}
    d = store.drivers.get(mm.get("driver_id") or "", {})
    order_rows.append({
        "order_id": o["id"], "day": mt.get("day"), "hour": mt.get("hour"), "hub_from": mt.get("hub_from"),
        "hub_to": mt.get("hub_to"), "truck_requested": mt.get("truck_requested"), "with_items": mt.get("with_items"),
        "status": "EXPIRED" if o["id"] in expired else o["status"], "matched": bool(o.get("driver_id")),
        "driver_truck": d.get("truck_type"), "pickup_km": mm.get("pickup_km"), "util": mm.get("util"),
        "offers": sum(1 for e in o["match_log"] if e["event"] == "offered"),
        "fail_reason": None if o.get("driver_id") else _fail_kind(o)})

if args.detail:
    def _group(key):
        g: dict = {}
        for r in order_rows:
            x = g.setdefault(r[key], {"orders": 0, "matched": 0, "km": []})
            x["orders"] += 1
            if r["matched"]:
                x["matched"] += 1
                if r["pickup_km"] is not None:
                    x["km"].append(r["pickup_km"])
        return {str(k): {"orders": v["orders"], "matched": v["matched"], "match_rate_pct": _pct(v["matched"], v["orders"]),
                         "avg_pickup_km": round(sum(v["km"]) / len(v["km"]), 1) if v["km"] else None}
                for k, v in sorted(g.items(), key=lambda kv: (kv[0] is None, kv[0]))}

    by_hour = {}
    for hh in range(24):
        rows = [r for r in order_rows if r["hour"] == hh]
        hs = [v for (dd, h2), v in hourly.items() if h2 == hh]
        offers_h = sum(v["offers"] for v in hs)
        lat_h = [x for v in hs for x in v["latency"]]
        by_hour[f"{hh:02d}"] = {"orders": len(rows), "matched": sum(r["matched"] for r in rows),
                                "match_rate_pct": _pct(sum(r["matched"] for r in rows), len(rows)),
                                "offers": offers_h, "rejection_rate_pct": _pct(sum(v["declined"] for v in hs), offers_h),
                                "latency_p95_ms": _pctl(lat_h, 0.95)}
    trucks: dict = {}
    for r in order_rows:
        if r["matched"]:
            t = trucks.setdefault(r["driver_truck"], {"assigned": 0, "util": [], "km": []})
            t["assigned"] += 1
            if r["util"] is not None:
                t["util"].append(r["util"])
            if r["pickup_km"] is not None:
                t["km"].append(r["pickup_km"])
    kms = [r["pickup_km"] for r in order_rows if r["matched"] and r["pickup_km"] is not None]
    utils = [r["util"] for r in order_rows if r["matched"] and r["util"] is not None]
    comp_rounds = [r for r in round_rows if r.get("has_choice") and r.get("deadhead_vs_nearest_pct") is not None]
    h_km = sum(r["hungarian_avg_km"] * r["assigned"] for r in comp_rounds)
    n_km = sum(r["nearest_avg_km"] * r["nearest_assigned"] for r in comp_rounds)
    h_n, n_n = sum(r["assigned"] for r in comp_rounds), sum(r["nearest_assigned"] for r in comp_rounds)
    g_rounds = [r for r in round_rows if r.get("has_choice") and r.get("deadhead_reduction_pct") is not None]
    fails: dict = {}
    for r in order_rows:
        if r["fail_reason"]:
            fails[r["fail_reason"]] = fails.get(r["fail_reason"], 0) + 1

    def _bins(xs, edges, fmt):
        out = {}
        for lo, hi in zip(edges, edges[1:] + [None]):
            out[fmt(lo, hi)] = sum(1 for x in xs if x >= lo and (hi is None or x < hi))
        return out

    metrics["detail"] = {
        "by_day": _group("day"), "by_hour": by_hour, "by_hub": _group("hub_from"),
        "by_truck": {k: {"assigned": v["assigned"], "avg_util_pct": round(100 * sum(v["util"]) / len(v["util"]), 1)
                         if v["util"] else None, "avg_pickup_km": round(sum(v["km"]) / len(v["km"]), 1) if v["km"] else None}
                     for k, v in sorted(trucks.items(), key=lambda kv: -kv[1]["assigned"])},
        "latency_ms": {"p50": _pctl(tick_latency, 0.5), "p95": _pctl(tick_latency, 0.95), "p99": _pctl(tick_latency, 0.99),
                       "max": round(max(tick_latency), 1) if tick_latency else None, "rounds": len(tick_latency)},
        "pickup_km_hist": _bins(kms, [0, 5, 10, 20, 30, 50], lambda lo, hi: f"{lo}~{hi}km" if hi else f"{lo}km 이상"),
        "util_hist": _bins([u * 100 for u in utils], [0, 40, 60, 80, 90],
                           lambda lo, hi: f"{lo}~{hi}%" if hi else f"{lo}% 이상"),
        "vs_baselines": {"comparable_rounds": len(comp_rounds), "all_rounds": len(round_rows),
                         "hungarian_avg_km": round(h_km / h_n, 2) if h_n else None,
                         "nearest_avg_km": round(n_km / n_n, 2) if n_n else None,
                         "vs_nearest_pct": round((n_km / n_n - h_km / h_n) / (n_km / n_n) * 100, 1)
                         if h_n and n_n and n_km else None,
                         "vs_greedy_pct_mean": round(sum(r["deadhead_reduction_pct"] for r in g_rounds) / len(g_rounds), 1)
                         if g_rounds else None},
        "fail_reasons": dict(sorted(fails.items(), key=lambda kv: -kv[1])),
    }
    det = metrics["detail"]
    section("대규모 상세 통계")
    print("  날짜별: " + " · ".join(f"{k}일 {v['orders']:,}건 ({v['match_rate_pct']}%)" for k, v in det["by_day"].items()))
    print(f"  배정 라운드 지연: p50 {det['latency_ms']['p50']}ms · p95 {det['latency_ms']['p95']}ms · "
          f"p99 {det['latency_ms']['p99']}ms · 최대 {det['latency_ms']['max']}ms")
    vb = det["vs_baselines"]
    print(f"  공차 거리: 헝가리안 {vb['hungarian_avg_km']}km vs 최단거리 {vb['nearest_avg_km']}km → {vb['vs_nearest_pct']}% "
          f"(선택지 있던 라운드 {vb['comparable_rounds']}/{vb['all_rounds']})")
    print("  미배차 사유: " + " · ".join(f"{k} {v:,}" for k, v in det["fail_reasons"].items()))

if args.csv_dir:
    os.makedirs(args.csv_dir, exist_ok=True)
    for name, rows in (("orders.csv", order_rows), ("rounds.csv", round_rows)):
        if not rows:
            continue
        path = os.path.join(args.csv_dir, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig: 엑셀에서 한글이 깨지지 않게
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"저장: {path} ({len(rows):,}행)")

print("\n" + json.dumps({k: v for k, v in metrics.items() if k != "detail"}, ensure_ascii=False, indent=2))
if args.out:
    # 보고서용(--out) + /admin/kpi 용(backend/data, 도커 이미지에 포함되는 위치)
    app_copy = os.path.join(ROOT, "backend", "data", "benchmark_metrics.json")
    # 기본 벤치마크(하루 · 배치 4)만 관제 KPI 사본으로 쓴다. 대규모 실행 결과는 --out 에만 저장
    standard = args.days == 1 and args.batch == 4 and not args.target and not args.expire_hours
    # KPI 기준 대규모 실행(--preset 8k)은 매칭 · 적재 KPI 용 사본을 따로 둔다
    large_copy = os.path.join(ROOT, "backend", "data", "benchmark_large.json") if args.preset == "8k" else None
    copies = [app_copy] if standard else ([large_copy] if large_copy else [])
    for path in dict.fromkeys([os.path.abspath(args.out)] + copies):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print(f"저장: {path}")
finish()
