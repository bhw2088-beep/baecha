"""
가상 화물 운송 텔레메트리 시뮬레이터
  기사 100명의 실시간 GPS 좌표(위도 · 경도) · 공차/적재 상태 · 차량 톤수(1톤 ~ 25톤) 패킷과
  화주의 실시간 운송 요청 패킷을 만들어 터미널에 실시간 표로 보여 주고, 서버에도 실제로 보낸다.

패킷 두 종류
  TELEMETRY      기사 1명 · 1초마다  {driver_id, 차종, 톤수, 상태(공차 · 제안 대기 · 적재), 위도, 경도, 속도, 화물}
  ORDER_REQUEST  화주 요청 · 포아송  {상차지, 하차지, 차종(필요 톤수), 운임, 냉장 여부}

서버 연동 (기본 · 시연 서버와 분리된 테스트 전용 스택 127.0.0.1:18000)
  기사 100명 가입 + 관제 승인 → 매 초:
    공차 기사 위치     POST /tracking/location/batch  → 매칭 거리 · PostGIS 반경 필터 · 관제 지도가 실시간 위치를 씀
    화주 요청          POST /shipper/orders           → 서버가 헝가리안으로 기사에게 제안
    제안 받은 기사     수락(확률) → 운행 시작 → 도로 경로를 따라 이동하며 POST /tracking/ping (Redis Streams)
    하차지 도착        POST /driver/runs/{id}/complete (전자 인수증) → 다시 공차
  --no-server 면 서버 없이 패킷만 (가까운 공차에게 직접 배정하는 간단한 흉내)

실행 (저장소 최상위 폴더 · Docker Desktop 필요 · 서버 연동 시 도커 이미지 baecha-backend:dev 필요)
    python scripts/telemetry.py                       # 기사 100명 · 5분 · 서버 연동 (VS Code 에서 ▷ 도 같음)
    python scripts/telemetry.py --minutes 10 --order-rate 1.0 --time-scale 60
    python scripts/telemetry.py --no-server           # 패킷만 (서버 · 도커 없이)
    Ctrl+C 로 언제든 멈추면 그때까지 요약을 남긴다
결과: 터미널 실시간 표 + scripts/results/telemetry_latest.json + 이 파일 맨 아래 ''' ''' 블록
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simulator as base  # noqa: E402 — 같은 폴더의 통합 시뮬레이터 (HTTP · 테스트 스택 · 기사 가입 함수 재사용)

SIM_KEY = "loadtest-sim-key"          # 테스트 스택의 시뮬레이터 키 (scripts/docker-compose.loadtest.yml)
TONS = {"1톤 카고": 1.0, "2.5톤 카고": 2.5, "5톤 윙바디": 5.0, "5톤 냉동탑차": 5.0,
        "11톤 탑차": 11.0, "11톤 냉동탑차": 11.0, "25톤 무진동": 25.0}
# 서버 없이(--no-server) 돌릴 때 쓰는 거점 좌표 (서버 /meta 와 같은 값)
FALLBACK_HUBS = [{"name": "서울/인천", "lat": 37.50, "lon": 126.80, "weight": 0.36},
                 {"name": "평택항", "lat": 36.97, "lon": 126.83, "weight": 0.16},
                 {"name": "대전 HUB", "lat": 36.35, "lon": 127.38, "weight": 0.14},
                 {"name": "부산항", "lat": 35.10, "lon": 129.04, "weight": 0.14},
                 {"name": "울산", "lat": 35.54, "lon": 129.31, "weight": 0.08},
                 {"name": "광주", "lat": 35.16, "lon": 126.85, "weight": 0.06},
                 {"name": "대구", "lat": 35.87, "lon": 128.60, "weight": 0.06}]
STATUS_LABEL = {"EMPTY": "공차", "OFFERED": "제안 대기", "LOADED": "적재(운행)", "UNLOADING": "하차 처리"}
POD_PHOTO = {"photo": ("pod.jpg", b"\xff\xd8\xff telemetry pod", "image/jpeg")}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 위경도 사이 직선거리(km)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Vehicle:
    """기사 1명 · 차량 1대의 상태 (위치 · 공차/적재 · 경로 위 진행)."""

    def __init__(self, did: str, token: str | None, truck: str, home: dict, rng: random.Random):
        self.id, self.token, self.truck, self.tons = did, token, truck, TONS.get(truck, 0.0)
        self.home = home                                         # 기본 대기 거점 (공차일 때 이 근처에 머묾)
        self.lat = home["lat"] + rng.gauss(0, 0.12)              # 거점 주변 가우스 분포 (σ ≈ 13km)
        self.lon = home["lon"] + rng.gauss(0, 0.12)
        self.status, self.speed, self.order_id = "EMPTY", 0.0, None
        self.route: list[tuple[float, float]] = []               # 운행 경로 [(lat, lon), ...]
        self.cum: list[float] = []                               # 경로 누적 거리(km)
        self.done_km = 0.0                                       # 경로 위 진행 거리(km)

    def set_route(self, points: list[tuple[float, float]]) -> None:
        """운행 시작: 도로 경로를 따라 이동할 준비 (누적 거리 계산)."""
        self.route = points or [(self.lat, self.lon)]
        self.cum = [0.0]
        for a, b in zip(self.route, self.route[1:]):
            self.cum.append(self.cum[-1] + haversine_km(a, b))
        self.done_km = 0.0
        self.lat, self.lon = self.route[0]

    def advance(self, km: float) -> bool:
        """경로를 km 만큼 따라감. 끝에 닿으면 True (하차지 도착)."""
        self.done_km = min(self.cum[-1], self.done_km + km)
        i = max(0, next((k for k, c in enumerate(self.cum) if c >= self.done_km), len(self.cum) - 1) - 1)
        seg = self.cum[i + 1] - self.cum[i] if i + 1 < len(self.cum) else 0
        f = (self.done_km - self.cum[i]) / seg if seg > 0 else 1.0
        a, b = self.route[i], self.route[min(i + 1, len(self.route) - 1)]
        self.lat, self.lon = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        return self.done_km >= self.cum[-1] - 1e-6

    def packet(self, plate: str = "") -> dict:
        """텔레메트리 패킷 (서버로 가는 값 + 화면 표시)."""
        return {"type": "TELEMETRY", "ts": time.strftime("%H:%M:%S"), "driver_id": self.id, "truck_type": self.truck,
                "tonnage": self.tons, "status": self.status, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "speed_kmh": round(self.speed, 1), "order_id": self.order_id}


class Telemetry:
    """시뮬레이션 본체: 차량 100대 · 화주 요청 · 서버 연동 · 실시간 표."""

    def __init__(self, a):
        self.a, self.rng = a, random.Random(a.seed)
        self.online = not a.no_server
        self.w = base.World(a.seed) if self.online else None
        self.hubs = FALLBACK_HUBS
        self.vehicles: dict[str, Vehicle] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=32)           # 서버 요청은 스레드에서 (화면 갱신이 막히지 않게)
        self.stats = {"telemetry": 0, "orders": 0, "orders_ok": 0, "orders_fail": 0, "offers": 0, "accepted": 0,
                      "declined": 0, "completed": 0, "pings": 0, "pings_ok": 0, "locations": 0}
        self.lat_ms: dict[str, list[float]] = {}                 # 엔드포인트별 응답 시간
        self.errors: dict[str, int] = {}
        self.recent_tel: list[dict] = []                         # 화면에 보여 줄 최근 패킷
        self.recent_req: list[dict] = []
        self.order_created: dict[str, float] = {}                # 주문 id → 생성 시각
        self.first_offer: dict[str, float] = {}                  # 주문 id → 첫 제안 시각
        self.seen: set[tuple[str, str]] = set()
        self.local_orders: list[dict] = []                       # --no-server: 아직 배정 안 된 요청
        self.t0 = time.time()

    # ------------------------------------------------------------------ 서버 호출 (응답 시간 · 오류 집계)
    def call(self, key: str, method: str, path: str, token: str | None = None, **kw):
        code, body, ms = base.call(method, path, token, **kw)
        with self.lock:
            self.lat_ms.setdefault(key, []).append(ms)
            if not 200 <= code < 300:
                self.errors[f"{key} {code}"] = self.errors.get(f"{key} {code}", 0) + 1
        return code, body

    # ------------------------------------------------------------------ 준비
    def setup(self) -> None:
        a = self.a
        if not self.online:
            for i in range(a.drivers):
                hub = self.rng.choices(self.hubs, weights=[h["weight"] for h in self.hubs])[0]
                truck = self.rng.choices(list(base.TRUCK_MIX), weights=list(base.TRUCK_MIX.values()))[0]
                self.vehicles[f"V-{i + 1:03d}"] = Vehicle(f"V-{i + 1:03d}", None, truck, hub, self.rng)
            return
        if not base.check_docker() or not base.stack_up():
            raise RuntimeError("테스트 스택을 준비하지 못했습니다")
        w, pw = self.w, base.demo_password()
        w.load_meta()
        self.hubs = w.hubs
        for role, email in (("admin", base.ADMIN_EMAIL), ("shipper", base.SHIPPER_EMAIL)):
            code, body, _ = base.call("POST", "/auth/login", json={"email": email, "password": pw})
            if code != 200:
                raise RuntimeError(f"{role} 로그인 실패: {base.detail(body)}")
            setattr(w, role, body["token"])
        base.log(f"기사 {a.drivers}명 가입 + 관제 승인 (노란 번호판 · 사업자번호 · 서류) ...")
        base.seed_drivers(w, a.drivers)
        by_name = {h["name"]: h for h in self.hubs}
        for did, d in w.drivers.items():
            # 등록 지역이 속한 거점 → 그 거점 주변에서 출발
            hub_name = next((h for h, prefixes in base.HUB_REGIONS.items()
                             if any(str(d["location"]).startswith(p) for p in prefixes)), self.hubs[0]["name"])
            self.vehicles[did] = Vehicle(did, d["token"], d["truck_type"], by_name.get(hub_name, self.hubs[0]), self.rng)

    # ------------------------------------------------------------------ 화주 요청 패킷
    def make_requests(self, dt: float) -> None:
        """포아송(평균 order_rate × dt 건)으로 화주 운송 요청 패킷 생성 → 서버로 주문 등록."""
        n, p, L = 0, 1.0, math.exp(-self.a.order_rate * dt)
        while True:                                            # 포아송 표본 (Knuth)
            p *= self.rng.random()
            if p <= L:
                break
            n += 1
        for _ in range(n):
            a_hub = self.rng.choices(self.hubs, weights=[h["weight"] or 0.1 for h in self.hubs])[0]
            b_hub = self.rng.choice([h for h in self.hubs if h["name"] != a_hub["name"]])
            truck = self.rng.choices(list(base.TRUCK_MIX), weights=list(base.TRUCK_MIX.values()))[0]
            pkt = {"type": "ORDER_REQUEST", "ts": time.strftime("%H:%M:%S"), "truck_type": truck,
                   "need_tons": TONS[truck], "cold_chain": "냉동" in truck,
                   "origin": self.w.region_near(a_hub["name"]) if self.online else a_hub["name"],
                   "destination": self.w.region_near(b_hub["name"]) if self.online else b_hub["name"]}
            with self.lock:
                self.stats["orders"] += 1
                self.recent_req = (self.recent_req + [pkt])[-3:]
            if self.online:
                self.pool.submit(self._post_order, pkt)
            else:
                pkt["origin_pt"] = (a_hub["lat"] + self.rng.gauss(0, 0.08), a_hub["lon"] + self.rng.gauss(0, 0.08))
                pkt["dest_pt"] = (b_hub["lat"] + self.rng.gauss(0, 0.08), b_hub["lon"] + self.rng.gauss(0, 0.08))
                pkt["created"] = time.time()
                self.local_orders.append(pkt)

    def _post_order(self, pkt: dict) -> None:
        w = self.w
        code, est = self.call("운임 추정", "POST", "/pricing/estimate", w.shipper,
                              json={"truck_type": pkt["truck_type"], "origin": pkt["origin"], "destination": pkt["destination"]})
        rec = est.get("recommended") if code == 200 and isinstance(est, dict) else None
        price = int((rec or 300_000) * self.rng.uniform(1.0, 1.1) / 5000) * 5000
        pkt["price"] = price
        code, body = self.call("주문 등록", "POST", "/shipper/orders", w.shipper,
                               json={"title": "[텔레메트리] 화주 요청", "truck_type": pkt["truck_type"],
                                     "origin": pkt["origin"], "destination": pkt["destination"],
                                     "price": price, "cold_chain": pkt["cold_chain"]})
        with self.lock:
            if code == 201 and isinstance(body, dict):
                self.stats["orders_ok"] += 1
                self.order_created[body["id"]] = time.time()
            else:
                self.stats["orders_fail"] += 1

    # ------------------------------------------------------------------ 제안 → 수락 → 운행 시작 (서버)
    def handle_offers(self) -> None:
        code, body = self.call("제안 확인", "GET", "/admin/offers", self.w.admin)
        if code != 200 or not isinstance(body, list):
            return
        for v in body:
            oid = v["id"]
            did = ((v.get("matching") or {}).get("driver") or {}).get("id")
            veh = self.vehicles.get(did)
            if not veh or (oid, did) in self.seen:
                continue
            self.seen.add((oid, did))
            with self.lock:
                self.stats["offers"] += 1
                self.first_offer.setdefault(oid, time.time())
                if veh.status == "EMPTY":
                    veh.status, veh.order_id = "OFFERED", oid
            self.pool.submit(self._respond, veh, oid)

    def _respond(self, veh: Vehicle, oid: str) -> None:
        time.sleep(self.rng.uniform(0.5, 2.0))                  # 기사가 제안을 보고 누르는 시간
        if self.rng.random() >= self.a.accept_rate:
            self.call("거절", "POST", f"/driver/offer/{oid}/decline", veh.token)
            with self.lock:
                self.stats["declined"] += 1
                if veh.order_id == oid:
                    veh.status, veh.order_id = "EMPTY", None
            return
        code, _ = self.call("수락", "POST", f"/driver/offer/{oid}/accept", veh.token)
        if code != 200:
            with self.lock:
                if veh.order_id == oid:
                    veh.status, veh.order_id = "EMPTY", None
            return
        code, body = self.call("운행 시작", "POST", f"/driver/runs/{oid}/start", veh.token)
        with self.lock:
            if code == 200 and isinstance(body, dict):
                self.stats["accepted"] += 1
                route = [(p[1], p[0]) for p in body.get("route") or []]          # [[lon, lat]] → (lat, lon)
                veh.set_route(route or [(body.get("lat") or veh.lat, body.get("lon") or veh.lon)])
                veh.status, veh.order_id, veh.speed = "LOADED", oid, self.rng.uniform(65, 90)
            elif veh.order_id == oid:
                veh.status, veh.order_id = "EMPTY", None

    def _complete(self, veh: Vehicle, oid: str) -> None:
        code, _ = self.call("하차 완료", "POST", f"/driver/runs/{oid}/complete", veh.token,
                            data={"receiver": "텔레메트리 인수자"}, files=POD_PHOTO)
        with self.lock:
            self.stats["completed"] += code == 200
            veh.status, veh.order_id, veh.speed = "EMPTY", None, 0.0

    # ------------------------------------------------------------------ 서버 없이: 가까운 공차에게 직접 배정
    def local_dispatch(self) -> None:
        for o in list(self.local_orders):
            empties = [v for v in self.vehicles.values() if v.status == "EMPTY" and v.tons >= o["need_tons"]]
            if not empties:
                continue
            v = min(empties, key=lambda x: haversine_km((x.lat, x.lon), o["origin_pt"]))
            self.local_orders.remove(o)
            oid = f"L-{self.stats['accepted'] + 1:04d}"
            self.first_offer[oid], self.order_created[oid] = time.time(), o["created"]
            self.stats["offers"] += 1
            self.stats["accepted"] += 1
            v.set_route([(v.lat, v.lon), o["origin_pt"], o["dest_pt"]])   # 상차지까지 공차 이동 → 하차지
            v.status, v.order_id, v.speed = "LOADED", oid, self.rng.uniform(65, 90)

    # ------------------------------------------------------------------ 한 틱: 이동 · 패킷 · 서버 전송
    def step(self, dt: float) -> None:
        km_per_h = self.a.time_scale * dt / 3600               # 시속 1km 일 때 이번 틱에 가는 거리
        empties, pings = [], []
        with self.lock:
            for v in self.vehicles.values():
                if v.status == "LOADED":
                    v.speed = max(55, min(95, v.speed + self.rng.uniform(-3, 3)))
                    if v.advance(v.speed * km_per_h):
                        if self.online:                    # 하차지 도착 → 하차 완료(인수증) 요청은 한 번만
                            v.status, v.speed = "UNLOADING", 0.0
                            self.pool.submit(self._complete, v, v.order_id)
                        else:
                            self.stats["completed"] += 1
                            v.status, v.order_id, v.speed = "EMPTY", None, 0.0
                    elif self.online:
                        pings.append((v, {"order_id": v.order_id, "lat": round(v.lat, 6), "lon": round(v.lon, 6),
                                          "speed": round(v.speed, 1)}))
                elif v.status == "UNLOADING":                  # 하차 처리 중: 제자리
                    pass
                else:                                          # 공차 · 제안 대기: 거점 근처를 천천히 움직임
                    v.speed = max(0.0, min(40.0, v.speed + self.rng.uniform(-8, 8))) if v.status == "EMPTY" else 0.0
                    step_deg = v.speed * km_per_h / 111
                    v.lat += self.rng.gauss(0, step_deg) + (v.home["lat"] - v.lat) * 0.01
                    v.lon += self.rng.gauss(0, step_deg) + (v.home["lon"] - v.lon) * 0.01
                    empties.append(v)
                pkt = v.packet()
                self.stats["telemetry"] += 1
                if self.rng.random() < 0.05:
                    self.recent_tel = (self.recent_tel + [pkt])[-5:]
        if not self.online:
            return
        # 공차 위치: 한 번에 (X-Sim-Key) → 서버 매칭이 실시간 공차 위치로 거리 계산
        locs = [{"driver_id": v.id, "lat": round(min(38.9, max(33.1, v.lat)), 6),
                 "lon": round(min(131.9, max(124.1, v.lon)), 6), "speed": round(v.speed, 1)} for v in empties]
        if locs:
            self.stats["locations"] += len(locs)
            self.pool.submit(self.call, "공차 위치", "POST", "/tracking/location/batch",
                             headers={"X-Sim-Key": SIM_KEY}, json={"locations": locs})
        for v, body in pings:                                    # 적재 기사: 기사 토큰으로 GPS 핑 (Redis Streams)
            self.pool.submit(self._ping, v.token, body)

    def _ping(self, token: str, body: dict) -> None:
        code, _ = self.call("GPS 핑", "POST", "/tracking/ping", token, json=body, timeout=10)
        with self.lock:
            self.stats["pings"] += 1
            self.stats["pings_ok"] += code == 202

    # ------------------------------------------------------------------ 화면
    def render(self) -> None:
        el = time.time() - self.t0
        with self.lock:
            vs = list(self.vehicles.values())
            st = dict(self.stats)
            tel, req = list(self.recent_tel), list(self.recent_req)
        by_status = {k: sum(1 for v in vs if v.status == k) for k in STATUS_LABEL}
        tons = sorted({v.tons for v in vs})
        lines = [f"가상 화물 운송 텔레메트리 — {'서버 연동 (테스트 스택 127.0.0.1:18000)' if self.online else '패킷만 (서버 없음)'} · "
                 f"경과 {int(el // 60)}분 {int(el % 60):02d}초 / {self.a.minutes}분 · 배속 ×{self.a.time_scale} · Ctrl+C 로 종료",
                 "",
                 f"기사 {len(vs)}명   " + " · ".join(f"{STATUS_LABEL[k]} {n}" for k, n in by_status.items()) +
                 f"   텔레메트리 패킷 {st['telemetry']:,}건 (초당 {st['telemetry'] / max(el, 1):.0f})",
                 "  톤수별   " + "  ".join(f"{t:g}톤: 공차 {sum(1 for v in vs if v.tons == t and v.status != 'LOADED')}"
                                         f" / 적재 {sum(1 for v in vs if v.tons == t and v.status == 'LOADED')}" for t in tons),
                 f"화주 요청 {st['orders']}건 " + (f"(서버 등록 {st['orders_ok']} · 실패 {st['orders_fail']})" if self.online
                                                  else "(가까운 공차에 바로 배정)") + f"   제안 {st['offers']} · "
                 f"수락 {st['accepted']} · 거절 {st['declined']} · 하차 완료 {st['completed']}"]
        if self.online:
            lines.append(f"서버 전송   GPS 핑 {st['pings']:,} (202 {st['pings_ok']:,}) · 공차 위치 {st['locations']:,}건 · "
                         + " · ".join(f"{k} p95 {base.pct(v, 95):.0f}ms" for k, v in list(self.lat_ms.items())[:6] if v))
            if self.errors:
                lines.append("  오류: " + " · ".join(f"{k} {n}" for k, n in list(self.errors.items())[:5]))
        lines += ["", "최근 텔레메트리 패킷"] + [
            "  " + json.dumps({k: p[k] for k in ("ts", "driver_id", "truck_type", "status", "lat", "lon", "speed_kmh",
                                                 "order_id")}, ensure_ascii=False) for p in tel]
        lines += ["최근 화주 운송 요청 패킷"] + [
            "  " + json.dumps({k: p.get(k) for k in ("ts", "origin", "destination", "truck_type", "need_tons",
                                                     "cold_chain", "price")}, ensure_ascii=False) for p in req]
        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------ 실행
    def run(self) -> dict:
        end = self.t0 + self.a.minutes * 60
        last = time.time()
        try:
            while time.time() < end:
                now = time.time()
                dt, last = now - last, now
                self.make_requests(dt)
                if self.online:
                    self.handle_offers()
                else:
                    self.local_dispatch()
                self.step(dt)
                self.render()
                time.sleep(max(0.0, self.a.tick - (time.time() - now)))
        except KeyboardInterrupt:
            print("\n사용자가 중단 (Ctrl+C) — 요약을 남깁니다")
        self.pool.shutdown(wait=True)
        return self.summary()

    def summary(self) -> dict:
        el = time.time() - self.t0
        waits = [self.first_offer[o] - self.order_created[o] for o in self.first_offer if o in self.order_created]
        vs = list(self.vehicles.values())
        return {
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.t0)), "elapsed_sec": round(el, 1),
            "mode": "서버 연동" if self.online else "패킷만", "drivers": len(vs), "options": vars(self.a),
            "stats": self.stats, "telemetry_per_sec": round(self.stats["telemetry"] / max(el, 1), 1),
            "final_status": {STATUS_LABEL[k]: sum(1 for v in vs if v.status == k) for k in STATUS_LABEL},
            "by_tonnage": {f"{t:g}톤": sum(1 for v in vs if v.tons == t) for t in sorted({v.tons for v in vs})},
            "order_to_offer_sec": {"count": len(waits), "p50": round(base.pct(waits, 50), 2) if waits else None,
                                   "p95": round(base.pct(waits, 95), 2) if waits else None},
            "server_latency": {k: base.ms_summary(v) for k, v in self.lat_ms.items()},
            "server_errors": self.errors,
            "sample_packets": {"telemetry": self.recent_tel[-2:], "order_request": self.recent_req[-2:]},
        }


def summary_text(s: dict) -> str:
    st = s["stats"]
    L = [f"## 텔레메트리 시뮬레이터 — {s['started']} · {s['elapsed_sec']}초 · {s['mode']}",
         f"- 기사 {s['drivers']}명 ({' · '.join(f'{k} {v}' for k, v in s['by_tonnage'].items())}) · "
         f"끝 상태 {' · '.join(f'{k} {v}' for k, v in s['final_status'].items())}",
         f"- 텔레메트리 패킷 {st['telemetry']:,}건 (초당 {s['telemetry_per_sec']}) · 화주 요청 {st['orders']}건 "
         f"(등록 {st['orders_ok']} · 실패 {st['orders_fail']})",
         f"- 제안 {st['offers']} · 수락 → 운행 {st['accepted']} · 거절 {st['declined']} · 하차 완료 {st['completed']} · "
         f"요청 → 첫 제안 p50 {s['order_to_offer_sec']['p50']} · p95 {s['order_to_offer_sec']['p95']}초"]
    if s["mode"] == "서버 연동":
        L.append(f"- 서버 전송: GPS 핑 {st['pings']:,}건 (202 {st['pings_ok']:,}) · 공차 위치 {st['locations']:,}건")
        for k, v in s["server_latency"].items():
            if v.get("count"):
                L.append(f"  {k}: {v['count']:,}회 · p50 {v['p50_ms']} · p95 {v['p95_ms']} · 최대 {v['max_ms']}ms")
        if s["server_errors"]:
            L.append("- 오류: " + " · ".join(f"{k} {n}" for k, n in s["server_errors"].items()))
    return "\n".join(L)


RESULT_MARK = "# ===== " + "마지막 실행 결과 (실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) ====="


def save(s: dict) -> None:
    """결과 JSON + 이 파일 맨 아래 블록 (코드가 잘리지 않게 마지막 표시줄 앞까지만 코드로 본다)."""
    base.RESULTS.mkdir(parents=True, exist_ok=True)
    (base.RESULTS / "telemetry_latest.json").write_text(json.dumps(s, ensure_ascii=False, indent=2, default=str),
                                                        encoding="utf-8")
    me = Path(__file__).resolve()
    src = me.read_text(encoding="utf-8")
    code = src[:src.rfind(RESULT_MARK)].rstrip() if RESULT_MARK in src else src.rstrip()
    if "def main(" not in code:
        return
    body = summary_text(s).replace("\\", "/").replace("'''", "’’’")
    me.write_text(f"{code}\n\n\n{RESULT_MARK}\n'''\n{body}\n'''\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="가상 화물 운송 텔레메트리 시뮬레이터")
    ap.add_argument("--drivers", type=int, default=100, help="기사 수 (기본 100)")
    ap.add_argument("--minutes", type=float, default=5, help="실행 시간(분, 기본 5)")
    ap.add_argument("--order-rate", type=float, default=0.5, help="화주 요청 초당 평균 건수 (포아송, 기본 0.5)")
    ap.add_argument("--accept-rate", type=float, default=0.9, help="기사 수락 확률 (기본 0.9)")
    ap.add_argument("--time-scale", type=float, default=60, help="이동 배속 (기본 ×60: 실제 1초 = 운행 1분)")
    ap.add_argument("--tick", type=float, default=1.0, help="패킷 주기(초, 기본 1)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-server", action="store_true", help="서버 없이 패킷만 (도커 불필요)")
    ap.add_argument("--keep", action="store_true", help="끝나도 테스트 스택을 지우지 않음")
    a = ap.parse_args()
    if os.name == "nt":
        os.system("")                                           # 윈도우 콘솔에서 화면 지우기(ANSI) 켜기
    sim = Telemetry(a)
    try:
        sim.setup()
        s = sim.run()
    except Exception as e:  # noqa: BLE001
        base.log(f"오류로 중단: {type(e).__name__}: {e}")
        s = sim.summary()
        s["error"] = f"{type(e).__name__}: {e}"
    finally:
        if sim.online:
            try:
                base.stack_down(a.keep)
            except Exception:  # noqa: BLE001
                pass
    print("\n" + summary_text(s) + "\n")
    save(s)
    return 0 if not s.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())


# ===== 마지막 실행 결과 (실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) =====
'''
## 텔레메트리 시뮬레이터 — 2026-11-18 11:57:17 · 300.3초 · 서버 연동
- 기사 100명 (1톤 13 · 2.5톤 23 · 5톤 39 · 11톤 20 · 25톤 5) · 끝 상태 공차 33 · 제안 대기 0 · 적재(운행) 67 · 하차 처리 0
- 텔레메트리 패킷 27,800건 (초당 92.6) · 화주 요청 132건 (등록 132 · 실패 0)
- 제안 124 · 수락 → 운행 108 · 거절 16 · 하차 완료 41 · 요청 → 첫 제안 p50 0.74 · p95 42.77초
- 서버 전송: GPS 핑 12,263건 (202 12,263) · 공차 위치 15,491건
  제안 확인: 278회 · p50 84.8 · p95 612.5 · 최대 1492.8ms
  공차 위치: 278회 · p50 140.3 · p95 811.8 · 최대 2654.1ms
  수락: 108회 · p50 120.7 · p95 816.8 · 최대 1114.9ms
  운행 시작: 108회 · p50 112.8 · p95 739.5 · 최대 1420.6ms
  거절: 16회 · p50 230.3 · p95 407.2 · 최대 1222.4ms
  운임 추정: 132회 · p50 286.2 · p95 747.2 · 최대 1195.8ms
  GPS 핑: 12,263회 · p50 122.5 · p95 816.7 · 최대 2390.3ms
  주문 등록: 132회 · p50 330.2 · p95 1391.6 · 최대 1988.0ms
  하차 완료: 41회 · p50 332.1 · p95 876.0 · 최대 1313.2ms
'''
