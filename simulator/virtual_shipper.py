"""
가상 화주 주문 생성 (부하 테스트 · 시연용 모의 데이터)

데모 화주 계정으로 로그인해, 지역 목록 · 3D 적재 샘플 중에서 무작위로 골라 화물을 등록한다.
등록 즉시 자동 매칭(PostGIS → LightGBM → 헝가리안)이 돈다.

실행 (도커):  docker compose run --rm simulator python -m simulator.virtual_shipper --count 5 --interval 3
실행 (로컬):  python -m simulator.virtual_shipper --count 5
옵션: --count 등록할 화물 수 (기본 5) · --interval 등록 간격 초 (기본 3) · --seed 난수 시드
등록한 화물은 관제 [데모 데이터 초기화]로 지울 수 있다.
"""

from __future__ import annotations

import argparse
import os
import random
import time

import requests

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")
SHIPPER_EMAIL = os.getenv("SIM_SHIPPER_EMAIL", "logistics@samsung.com")   # core/seed.py 데모 화주
SHIPPER_PASSWORD = os.getenv("SIM_SHIPPER_PASSWORD", "baecha1234")        # core/seed.py DEMO_PASSWORD

TITLES = ["반도체 부품", "생활가전", "냉동 수산물", "의약품 콜드체인", "자동차 부품", "건축 자재", "음료 팔레트", "택배 간선"]


def call(method: str, path: str, token: str | None = None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=30, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:200]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser(description="가상 화주 주문 생성")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--interval", type=float, default=3)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    token = call("POST", "/auth/login", json={"email": SHIPPER_EMAIL, "password": SHIPPER_PASSWORD})["token"]
    meta = call("GET", "/meta", token)
    regions = [r["value"] if isinstance(r, dict) else r for r in meta["regions"]]
    samples = list(call("GET", "/packing/meta", token)["samples"].values())

    ok = 0
    for i in range(1, args.count + 1):
        origin, dest = rng.sample(regions, 2)
        sample = rng.choice(samples)
        try:
            plan = call("POST", "/packing/plan", token, json={"items": sample["items"], "truck_type": None,
                                                              "cold_chain": sample["cold_chain"]})
            truck = plan["truck"]["name"]
            est = call("POST", "/pricing/estimate", token, json={
                "truck_type": truck, "origin": origin, "destination": dest, "cold_chain": sample["cold_chain"],
                "weight_kg": plan["metrics"]["loaded_weight"]})
            price = int(est["recommended"] * rng.uniform(0.92, 1.12) / 5000) * 5000
            o = call("POST", "/shipper/orders", token, json={
                "title": f"[가상] {rng.choice(TITLES)}", "truck_type": truck, "origin": origin, "destination": dest,
                "price": price, "cold_chain": sample["cold_chain"], "urgent": rng.random() < 0.2,
                "items": sample["items"]})
            m = o["matching"] or {}
            who = f" → {m['driver']['name']}" if m.get("driver") else ""
            print(f"[{i}/{args.count}] {o['id']} {origin} → {dest} · {truck} · {price:,}원 · {m.get('state')}{who}",
                  flush=True)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{args.count}] 실패: {e}", flush=True)
        if i < args.count:
            time.sleep(args.interval)
    print(f"완료: {ok}/{args.count}건 등록", flush=True)


if __name__ == "__main__":
    main()
