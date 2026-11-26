"""
가상 화주 주문 생성 (Phase 1 · 6: 공간적 클러스터링 + 포아송 분포 · 모의 데이터)

[위치]  전국에 무작위로 뿌리지 않는다. 물류 거점(서울/인천 · 평택항 · 대전 HUB · 부산항 · 울산 · 대구 · 광주)을
        중심으로 정규분포(가우스 노이즈, σ≈13km) 좌표를 만든다 → 거점 주변에 주문이 몰린다.
        거점 좌표 · 비중은 backend/engines/matcher/spatial.py 의 HUBS · HUB_WEIGHTS 하나를 쓴다 (관제 지도와 같은 거점).
        만든 좌표는 '가장 가까운 등록 지역 주소'로 붙인다 (도로 경로 캐시가 주소 단위라서).
[시간]  시간대별 포아송 도착률 λ(h) = 기본 콜 × 시간대 배율 (engines/pricing_engine.py HOUR_PROFILE)
        러시아워(08~09시 · 18~19시)에 콜이 폭증, 새벽엔 거의 없음. 콜 간격은 지수분포.
[위반]  SIM_BAD_CARGO_RATE(기본 12%) 확률로 일부러 규칙을 어기는 화물을 섞는다 → 서버가 거부하는지 검증
        · 과적: 화주가 고른 차종 최대 적재량의 110~120% 무게
        · 편심: 최대 적재량의 45~55% 인 장비 1대가 적재함 앞쪽에 몰려 전후 무게중심 15% 미만 (R10 축중량 위반)

실행 (도커):  docker compose run --rm simulator python -m simulator.virtual_shipper --count 5
             docker compose run --rm simulator python -m simulator.virtual_shipper --timeline --hour-sec 5
실행 (로컬):  python -m simulator.virtual_shipper --count 5
             python -m simulator.virtual_shipper --dry-run          # 등록 없이 24시간 콜 타임라인만 출력
옵션
  --count N      N건 등록 (지금 시각의 도착률로 간격 결정)
  --timeline     하루 24시간을 압축해 시간대별 포아송 콜 수만큼 등록 (--hour-sec: 1시간 = 몇 초, 기본 10)
  --dry-run      서버 없이 시간대별 콜 수 · 거점 분포만 출력
  --seed         난수 시드
등록한 화물은 관제 [데모 데이터 초기화]로 지울 수 있다.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from backend.engines.matcher.spatial import HUB_WEIGHTS, REGION_COORDS, REGION_LABELS, haversine_km  # noqa: E402
from backend.engines.matcher.spatial import HUBS as MAP_HUBS  # noqa: E402
from backend.engines.packer.trucks import TRUCKS  # noqa: E402
from backend.engines.pricing_engine import HOUR_PROFILE, RUSH_HOURS, poisson  # noqa: E402

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")
SHIPPER_EMAIL = os.getenv("SIM_SHIPPER_EMAIL", "logistics@hanbit.example.com")   # core/seed.py 데모 화주
SHIPPER_PASSWORD = os.getenv("SIM_SHIPPER_PASSWORD", "baecha1234")        # core/seed.py DEMO_PASSWORD

TITLES = ["반도체 부품", "생활가전", "냉동 수산물", "의약품 콜드체인", "자동차 부품", "건축 자재", "음료 팔레트", "택배 간선"]

# 물류 거점 (중심 좌표 · 주문 비중) — 관제 지도와 같은 거점 (spatial.HUBS · HUB_WEIGHTS)
HUBS = {name: (MAP_HUBS[name], w) for name, w in HUB_WEIGHTS.items()}
SIGMA_DEG = 0.12          # 약 13km
BAD_CARGO_RATE = float(os.getenv("SIM_BAD_CARGO_RATE", "0.12"))   # 위반 화물 주입 비율 (과적 · 편심 반반)
# 편심 시나리오는 적재함이 충분히 길어야 '앞쪽 쏠림'이 축중량 위반이 된다 (1톤 · 2.5톤은 짧아서 제외)
ECCENTRIC_TRUCKS = ["5톤 윙바디", "11톤 탑차", "25톤 무진동"]
BASE_CALLS_PER_HOUR = 4   # 기본 콜 수 (시간대 배율을 곱해 λ)


def pick_hub(rng: random.Random, exclude: str | None = None) -> str:
    names = [h for h in HUBS if h != exclude]
    return rng.choices(names, weights=[HUBS[h][1] for h in names])[0]


def gaussian_point(hub: str, rng: random.Random) -> tuple[float, float]:
    (lat, lon), _ = HUBS[hub]
    return lat + rng.gauss(0, SIGMA_DEG), lon + rng.gauss(0, SIGMA_DEG)


def nearest_region(p: tuple[float, float]) -> str:
    """좌표 → 가장 가까운 드롭다운 지역 주소."""
    return min(REGION_LABELS, key=lambda a: haversine_km(p, REGION_COORDS[REGION_LABELS[a]]))


def make_route(rng: random.Random) -> dict:
    """상차 거점 → 하차 거점 (서로 다른 거점), 각각 가우스 좌표 → 주소."""
    a = pick_hub(rng)
    b = pick_hub(rng, exclude=a)
    pa, pb = gaussian_point(a, rng), gaussian_point(b, rng)
    origin, dest = nearest_region(pa), nearest_region(pb)
    if origin == dest:
        dest = nearest_region(HUBS[b][0])
    while origin == dest:  # 드물게 하차 거점 중심도 같은 지역이면, 다른 지역이 나올 때까지 하차 좌표를 다시 뽑는다
        pb = gaussian_point(b, rng)
        dest = nearest_region(pb)
    return {"hub_from": a, "hub_to": b, "origin": origin, "destination": dest, "origin_point": pa, "dest_point": pb}


def bad_cargo(rng: random.Random, kind: str | None = None) -> tuple[str, str, list[dict]]:
    """검증용 위반 화물 세트 → (종류 'overload' | 'eccentric', 차종, 품목). 서버 적재 규칙이 거부해야 정상."""
    kind = kind or rng.choice(["overload", "eccentric"])
    if kind == "overload":
        truck = rng.choice([t for t in TRUCKS if not TRUCKS[t].refrigerated])
        total = TRUCKS[truck].max_weight * rng.uniform(1.10, 1.20)      # 최대 적재량의 110~120%
        n = rng.randint(8, 16)
        return kind, truck, [dict(name="과적 강재 블록", length=500, width=500, height=500, weight=round(total / n),
                                  qty=n, upright=True, stackable=True, max_top_load=None, drop=1)]
    truck = rng.choice(ECCENTRIC_TRUCKS)
    heavy = TRUCKS[truck].max_weight * rng.uniform(0.45, 0.55)           # 장비 1대가 적재량의 절반
    return kind, truck, [
        dict(name="편심 중장비 (한쪽 쏠림)", length=1200, width=1000, height=1200, weight=round(heavy), qty=1,
             upright=True, stackable=True, max_top_load=30, drop=1),
        dict(name="경량 부속 박스", length=600, width=400, height=400, weight=12, qty=rng.randint(4, 8),
             upright=False, stackable=True, max_top_load=60, drop=1),
    ]


def rejection_reasons(plan: dict) -> list[str]:
    """적재 계획 → 서버가 등록을 거부하는 사유 (미적재 사유 + 필수 규칙 위반). 빈 목록이면 등록 가능."""
    out = sorted({u["reason"] for u in plan.get("unplaced", [])})
    out += [f"{c.get('code', '')} {c['name']}".strip() for c in plan["constraints"] if c["status"] == "fail"]
    return out


def hourly_calls(rng: random.Random) -> list[int]:
    """하루 24시간 시간대별 콜 수 (포아송, λ = 기본 × 시간대 배율)."""
    return [poisson(BASE_CALLS_PER_HOUR * HOUR_PROFILE[h], rng) for h in range(24)]


def call(method: str, path: str, token: str | None = None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=30, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:200]}")
    return r.json()


def dry_run(rng: random.Random):
    calls = hourly_calls(rng)
    print("시간대별 포아송 콜 수 (λ = 기본 4 × 시간대 배율)")
    for h, n in enumerate(calls):
        mark = " ◀ 러시아워" if h in RUSH_HOURS else ""
        print(f"  {h:02d}시  λ={BASE_CALLS_PER_HOUR * HOUR_PROFILE[h]:4.1f}  {'■' * n:<16} {n}{mark}")
    print(f"하루 합계 {sum(calls)}건")
    counts: dict[str, int] = {}
    for _ in range(300):
        r = make_route(rng)
        counts[r["hub_from"]] = counts.get(r["hub_from"], 0) + 1
    print("\n상차 거점 분포 (300건 표본)")
    for h, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {h:<8} {'■' * (n // 5)} {n}")
    r = make_route(rng)
    print(f"\n예: {r['hub_from']} 주변 {r['origin_point'][0]:.3f},{r['origin_point'][1]:.3f} → '{r['origin']}' / "
          f"{r['hub_to']} 주변 → '{r['destination']}'")


def register_bad(token: str, rng: random.Random, label: str) -> str:
    """위반 화물 등록 시도 → 서버가 400 으로 거부해야 정상 (거부되면 '차단', 등록되면 '놓침')."""
    route = make_route(rng)
    kind, truck, items = bad_cargo(rng)
    label_kind = "과적" if kind == "overload" else "편심"
    r = requests.post(f"{API_BASE}/shipper/orders", headers={"Authorization": f"Bearer {token}"}, timeout=30, json={
        "title": f"[가상 · {label_kind} 검증] {rng.choice(TITLES)}", "truck_type": truck, "origin": route["origin"],
        "destination": route["destination"], "price": 500_000, "items": items})
    if r.status_code == 400:
        return f"{label} 🚫 차단 ({label_kind} · {truck}): {r.json().get('detail', '')[:120]}"
    return f"{label} ⚠ 놓침 ({label_kind} · {truck}) → 등록됨 {r.status_code}"


def register(token: str, rng: random.Random, samples: list[dict], label: str) -> str:
    if rng.random() < BAD_CARGO_RATE:
        return register_bad(token, rng, label)
    route = make_route(rng)
    sample = rng.choice(samples)
    plan = call("POST", "/packing/plan", token, json={"items": sample["items"], "truck_type": None,
                                                      "cold_chain": sample["cold_chain"]})
    truck = plan["truck"]["name"]
    est = call("POST", "/pricing/estimate", token, json={
        "truck_type": truck, "origin": route["origin"], "destination": route["destination"],
        "cold_chain": sample["cold_chain"], "weight_kg": plan["metrics"]["loaded_weight"]})
    price = int(est["recommended"] * rng.uniform(0.92, 1.12) / 5000) * 5000
    o = call("POST", "/shipper/orders", token, json={
        "title": f"[가상] {rng.choice(TITLES)}", "truck_type": truck, "origin": route["origin"],
        "destination": route["destination"], "price": price, "cold_chain": sample["cold_chain"],
        "urgent": rng.random() < 0.2, "items": sample["items"]})
    m = o["matching"] or {}
    who = f" → {m['driver']['name']}" if m.get("driver") else ""
    mk = est.get("market") or {}
    surge = f" · 서지 +{mk['surge']:.0%}" if mk.get("surge") else ""
    return (f"{label} {o['id']} [{route['hub_from']}→{route['hub_to']}] {route['origin']} → {route['destination']} · "
            f"{truck} · {price:,}원{surge} · {m.get('state')}{who}")


def main():
    ap = argparse.ArgumentParser(description="가상 화주 주문 생성 (가우스 클러스터링 + 포아송)")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--timeline", action="store_true")
    ap.add_argument("--hour-sec", type=float, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--interval", type=float, default=None, help="(예전 옵션) 고정 간격 초")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    if args.dry_run:
        dry_run(rng)
        return

    token = call("POST", "/auth/login", json={"email": SHIPPER_EMAIL, "password": SHIPPER_PASSWORD})["token"]
    samples = list(call("GET", "/packing/meta", token)["samples"].values())
    ok = fail = 0
    if args.timeline:
        calls = hourly_calls(rng)
        print(f"하루 {sum(calls)}건 · 1시간 = {args.hour_sec}초", flush=True)
        for h, n in enumerate(calls):
            print(f"── {h:02d}시 λ={BASE_CALLS_PER_HOUR * HOUR_PROFILE[h]:.1f} → {n}건", flush=True)
            for k in range(n):
                try:
                    print(register(token, rng, samples, f"  [{h:02d}시 {k + 1}/{n}]"), flush=True)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    print(f"  실패: {e}", flush=True)
                time.sleep(args.hour_sec / max(n, 1))
            if n == 0:
                time.sleep(args.hour_sec)
    else:
        lam = BASE_CALLS_PER_HOUR * HOUR_PROFILE[datetime.now().hour]  # 지금 시각의 시간당 콜
        for i in range(1, args.count + 1):
            try:
                print(register(token, rng, samples, f"[{i}/{args.count}]"), flush=True)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[{i}/{args.count}] 실패: {e}", flush=True)
            if i < args.count:
                # 포아송 과정의 도착 간격 = 지수분포 (평균 3600/λ 초를 시연용으로 1/600 압축)
                gap = args.interval if args.interval is not None else min(8.0, rng.expovariate(lam / 6.0))
                time.sleep(gap if math.isfinite(gap) else 3)
    print(f"완료: {ok}건 등록 · {fail}건 실패", flush=True)


if __name__ == "__main__":
    main()
