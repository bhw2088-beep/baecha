"""
Phase 6: 마이크로 배치 매칭 스트레스 테스트 + GPS 디바운싱 오탐 검증 (서버 없이, 메모리 저장소)

시나리오
  1) 가상 기사 군집: 물류 거점 주변 가우스 분포 위치 · 표준 톤수(1 · 2.5 · 5 · 11 · 25톤) · 사업용 번호판
  2) 주문: 하루 24시간을 압축, 시간대별 포아송 콜(러시아워 폭증) · 거점 클러스터링 (simulator/virtual_shipper.py)
  3) 마이크로 배치: 한 '틱'(3~5초 창)마다 쌓인 주문을 한 번에 헝가리안 배정 (Redis 분산 락 경로 그대로)
  4) 기사 응답: 로지스틱 확률 규칙(거리 · 적재율 · 운임 · 권역 선호 · 평점 · 러시아워)으로 수락/거절
  5) 적재 오탐: 배차된 화물마다 배정 차량 기준 적재 계획을 다시 검증 → 안전율 · 축중량 · 중량 위반 0건인지
  6) GPS: 정상 지터 · 단발 튐(GPS 스파이크) · 고의 이탈(연속 4회)을 섞어 핑 → 오탐 0건, 이탈은 정확히 3번째에 경고

실행: 수정본 폴더에서
    python tests/stress_test.py                    # 기본 (주문 120 · 기사 45)
    python tests/stress_test.py --quick            # CI 용 (주문 30 · 기사 18)
    python tests/stress_test.py --out docs/benchmark_metrics.json
결과: '결과: N개 통과 · 0개 실패' + 메트릭 JSON
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

from _check import check, finish, section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.database import Store  # noqa: E402
from backend.engines import compliance_engine as comp  # noqa: E402
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
args = ap.parse_args()
if args.quick:
    args.orders, args.drivers = 30, 18
rng = random.Random(args.seed)

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
calls = vs.hourly_calls(rng)
total_calls = sum(calls) or 1
per_hour = [max(0, round(c * args.orders / total_calls)) for c in calls]
rush = per_hour[8] + per_hour[18]
check("러시아워(08 · 18시) 콜이 새벽(02~04시)보다 많음", rush > sum(per_hour[2:5]), f"러시아워 {rush} · 새벽 {sum(per_hour[2:5])}")

samples = list(SAMPLES.values())


def new_order(h: int, k: int) -> str:
    route = vs.make_route(rng)
    use_items = rng.random() < 0.35
    if use_items:
        smp = rng.choice(samples)
        from backend.engines.packer.engine import Options
        from backend.engines.packer.models import ItemSpec
        from backend.engines.packer.service import plan
        res = plan([ItemSpec(**i) for i in smp["items"]], truck_type=None, cold_chain=smp["cold_chain"],
                   options=Options(record_steps=False))
        truck, cold = res["truck"]["name"], smp["cold_chain"]
    else:
        truck = rng.choice([t for t, _ in TRUCK_MIX])
        cold, res, smp = truck in pricing.COLD_TYPES, None, None
    est = pricing.estimate(truck, route["origin"], route["destination"], cold_chain=cold)
    price = int(est["recommended"] * rng.uniform(0.9, 1.12) / 5000) * 5000
    o = store.create_order("S-001", {"title": f"[스트레스 {h:02d}시] {k}", "truck_type": truck,
                                     "origin": route["origin"], "destination": route["destination"],
                                     "price": price, "cold_chain": cold, "urgent": rng.random() < 0.15},
                           packing=res if res and not res["unplaced"] else None,
                           items=smp["items"] if smp else None)
    return o["id"]


# ------------------------------------------------------------------ 3~4) 마이크로 배치 + 로지스틱 응답
section("마이크로 배치 헝가리안 + 로지스틱 수락/거절")
offers = accepted = declined = 0
t_start = time.perf_counter()
tick_latency = []
for h, n in enumerate(per_hour):
    for k in range(n):
        new_order(h, k)
        if (k + 1) % 4 == 0 or k == n - 1:   # 주문 4건(≈3~5초 창)마다 한 번 배정 라운드
            t0 = time.perf_counter()
            store._matching_round(datetime.now())
            tick_latency.append((time.perf_counter() - t0) * 1000)
            for o in [o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"]]:
                did = o["offer"]["driver_id"]
                d = store.drivers[did]
                feats = store._ml_features(o, did, o["offer"]["pickup_km"], o["offer"]["util"] or 0.5,
                                           store._accept_rates())
                offers += 1
                if rng.random() < ml_ranker._true_accept_prob(feats):   # 로지스틱 확률 규칙
                    store.accept_offer(did, o["id"])
                    accepted += 1
                else:
                    o["declined"].append(did)
                    o["offer"] = None
                    store._log(o, "declined", did, datetime.now(), note="스트레스 테스트 로지스틱 거절")
                    declined += 1
            # 운행 완료 처리 → 기사를 하차지 공차로 되돌린다 (공급 순환)
            for o in list(store.orders.values()):
                if o["status"] == "ASSIGNED" and o["title"].startswith("[스트레스") and rng.random() < 0.6:
                    store.start_run(o["driver_id"], o["id"])
                    store.complete_run(o["driver_id"], o["id"], "인수자", photo={"filename": "pod.jpg", "size": 1024, "sha256": "ab" * 32})
elapsed = time.perf_counter() - t_start
m = store.matching_metrics()
lat = sorted(tick_latency)
p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else 0
stress_orders = [o for o in store.orders.values() if o["title"].startswith("[스트레스")]
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

metrics = {
    "generated_at": datetime.now().isoformat(timespec="seconds"), "seed": args.seed,
    "drivers": args.drivers, "orders": len(stress_orders), "rounds": len(tick_latency),
    "orders_per_hour": per_hour,
    "matched": len(matched), "match_rate_pct": round(100 * len(matched) / max(len(stress_orders), 1), 1),
    "offers": offers, "accepted": accepted, "declined": declined, "rejection_rate_pct": rej_rate,
    "dispatch_latency_p95_ms": round(p95, 1), "dispatch_latency_p95_sec": round(p95 / 1000, 4),
    "dispatch_latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
    "empty_deadhead_km_reduction_pct": m["empty_deadhead_km_reduction_pct"],
    "threshold_filtered_pairs": m["threshold_filtered"], "ml_threshold": m["ml_threshold"],
    "packing_checked": checked, "packing_violations": violations,
    "overload_rate_pct": round(100 * violations / checked, 2) if checked else 0.0,
    "gps": {"runs": len(seed_runs[:8]), "spikes": spikes, "false_alarms": false_alarms, "deviation_runs": dev_runs,
            "detected": detected, "alert_ping_index": alert_at, "engine": "Shapely" if te.HAS_SHAPELY else "python"},
    "elapsed_sec": round(elapsed, 1),
}
print("\n" + json.dumps(metrics, ensure_ascii=False, indent=2))
if args.out:
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print(f"저장: {args.out}")
finish()
