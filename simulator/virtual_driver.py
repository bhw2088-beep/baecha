"""
Phase 6: 가상 기사 GPS 시뮬레이터 (모의 데이터)

운행 중인 화물마다 '가상 기사 단말'이 도로 경로를 따라 달리며 실시간 핑을 보낸다.
실제 차량 GPS 는 쓰지 않는다 — 모든 위치는 이 시뮬레이터가 만든 모의 데이터다.

  1) 2초마다 백엔드 내부 API(/internal/runs)에서 운행 목록(도로 경로 · 진행률 · 정체 여부)을 받는다
  2) 지난 시간 × 배속(TIME_SCALE) × 속도만큼 경로 위에서 앞으로 이동 (속도 60~98km/h 사이에서 흔들림)
  3) GPS 튀김: 위경도에 가우스 노이즈(Gaussian Jitter, 표준편차 8m)
  4) 고의 이탈 시나리오: SIM_DEVIATION_EVERY 초마다 운행 1대가 경로 옆 2km 로 4번 연속 벗어났다가 복귀
     → 서버는 연속 3번째 핑에서 정확히 한 번 '경로 이탈' 경고 (디바운싱 검증)
  5) POST /tracking/ping/batch (X-Sim-Key) → 서버가 배정 기사 대조 → Redis Streams XADD → 워커 판정

환경변수: API_BASE · SIM_API_KEY · TIME_SCALE · SIM_INTERVAL(2초) · SIM_JITTER_M(8) ·
          SIM_DEVIATION_EVERY(240초, 0 이면 끔) · SIM_DEVIATION_KM(2.0) · SIM_REOUTSOURCE_EVERY(0=끔, 재위탁 핑 시나리오)
실행: docker compose 의 simulator 서비스가 자동 실행
     로컬: python -m simulator.virtual_driver
"""

from __future__ import annotations

import math
import os
import random
import sys
import time
from datetime import datetime

import requests

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")
SIM_API_KEY = os.getenv("SIM_API_KEY", "sim-dev-key")
TIME_SCALE = int(os.getenv("TIME_SCALE", "20"))
INTERVAL = float(os.getenv("SIM_INTERVAL", "2"))
JITTER_M = float(os.getenv("SIM_JITTER_M", "8"))
DEVIATION_EVERY = float(os.getenv("SIM_DEVIATION_EVERY", "240"))
DEVIATION_KM = float(os.getenv("SIM_DEVIATION_KM", "2.0"))
DEVIATION_PINGS = 4
REOUTSOURCE_EVERY = float(os.getenv("SIM_REOUTSOURCE_EVERY", "0"))


def _km(a, b) -> float:
    """[lon, lat] 두 점 사이 거리(km)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def point_along(route: list[list[float]], ratio: float) -> tuple[float, float, int]:
    """도로 경로([[lon, lat], ...])의 ratio(0~1) 지점 → (lat, lon, 구간 번호)."""
    if len(route) < 2:
        return route[0][1], route[0][0], 0
    segs = [_km(route[i], route[i + 1]) for i in range(len(route) - 1)]
    target = sum(segs) * max(0.0, min(1.0, ratio))
    for i, s in enumerate(segs):
        if target <= s or i == len(segs) - 1:
            t = 0 if s == 0 else min(1.0, target / s)
            lon = route[i][0] + (route[i + 1][0] - route[i][0]) * t
            lat = route[i][1] + (route[i + 1][1] - route[i][1]) * t
            return lat, lon, i
        target -= s
    return route[-1][1], route[-1][0], len(route) - 2


def jitter(lat: float, lon: float, rng: random.Random, sigma_m: float = JITTER_M) -> tuple[float, float]:
    """GPS 튀김: 가우스 노이즈 (미터 → 도)."""
    return (lat + rng.gauss(0, sigma_m) / 110_574,
            lon + rng.gauss(0, sigma_m) / (111_320 * math.cos(math.radians(lat))))


def offset(route: list[list[float]], i: int, lat: float, lon: float, km: float) -> tuple[float, float]:
    """진행 방향 오른쪽으로 km 만큼 벗어난 점 (고의 이탈)."""
    a, b = route[i], route[min(i + 1, len(route) - 1)]
    dx = (b[0] - a[0]) * 111.320 * math.cos(math.radians(lat))
    dy = (b[1] - a[1]) * 110.574
    n = math.hypot(dx, dy) or 1.0
    ox, oy = dy / n * km, -dx / n * km
    return lat + oy / 110.574, lon + ox / (111.320 * math.cos(math.radians(lat)))


class VirtualDriver:
    """운행 한 건을 맡은 가상 기사 단말."""

    def __init__(self, run: dict, rng: random.Random):
        self.order_id, self.driver_id = run["order_id"], run["driver_id"]
        self.progress = run["progress"]
        self.speed = run["speed"] or rng.randint(70, 90)
        self.rng = rng
        self.detour = 0  # 남은 고의 이탈 핑 수

    def step(self, run: dict, dt: float) -> dict:
        self.progress = max(self.progress, run["progress"])  # 서버 쪽 값이 더 앞서 있으면 따라간다
        if run["stopped"]:
            self.speed = 0  # 관제가 '장시간 정체'로 잡은 차량은 멈춰 있음
        else:
            self.speed = max(60, min(98, (self.speed or 78) + self.rng.randint(-4, 4)))
            km = dt * TIME_SCALE / 3600 * self.speed
            self.progress = min(97.0, self.progress + km / max(run["distance_km"], 1) * 100)
        lat, lon, i = point_along(run["route"], self.progress / 100)
        tag = "normal"
        if self.detour > 0:
            lat, lon = offset(run["route"], i, lat, lon, DEVIATION_KM)
            self.detour -= 1
            tag = "deviation"
        lat, lon = jitter(lat, lon, self.rng)
        return {"order_id": self.order_id, "driver_id": self.driver_id or None, "lat": round(lat, 6),
                "lon": round(lon, 6), "speed": self.speed, "ts": datetime.now().isoformat(timespec="seconds"),
                "_tag": tag}


def main():
    rng = random.Random(11)
    http = requests.Session()
    http.headers["X-Sim-Key"] = SIM_API_KEY
    drivers: dict[str, VirtualDriver] = {}
    sent, last = 0, time.monotonic()
    next_detour = time.monotonic() + (DEVIATION_EVERY or float("inf"))
    next_foreign = time.monotonic() + (REOUTSOURCE_EVERY or float("inf"))
    print(f"[virtual_driver] API {API_BASE} · {INTERVAL}s 간격 · 배속 x{TIME_SCALE} · 지터 {JITTER_M}m · "
          f"고의 이탈 {'매 ' + str(int(DEVIATION_EVERY)) + '초' if DEVIATION_EVERY else '끔'}", flush=True)
    while True:
        try:
            runs = http.get(f"{API_BASE}/internal/runs", timeout=5).json()
        except Exception as e:  # noqa: BLE001 — 백엔드가 아직 안 켜졌거나 재시작 중
            print(f"[virtual_driver] 백엔드 대기 중: {e}", flush=True)
            time.sleep(3)
            continue
        if isinstance(runs, dict):  # 오류 응답 (키 불일치 등)
            print(f"[virtual_driver] API 오류: {runs}", flush=True)
            time.sleep(5)
            continue
        now = time.monotonic()
        dt, last = now - last, now
        alive = {r["order_id"] for r in runs}
        for run in runs:
            drivers.setdefault(run["order_id"], VirtualDriver(run, rng))
        moving = [r for r in runs if not r["stopped"]]
        if moving and now >= next_detour:
            target = rng.choice(moving)["order_id"]
            drivers[target].detour = DEVIATION_PINGS
            next_detour = now + DEVIATION_EVERY
            print(f"[virtual_driver] 고의 이탈 시나리오 → {target} ({DEVIATION_KM}km × {DEVIATION_PINGS}회)", flush=True)
        pings = [drivers[r["order_id"]].step(r, dt) for r in runs]
        if pings and now >= next_foreign:
            p = dict(rng.choice(pings))
            p["driver_id"] = "D-999"  # 배정 기사가 아닌 단말 → 서버가 재위탁 의심으로 거절
            pings.append(p)
            next_foreign = now + REOUTSOURCE_EVERY
        if pings:
            try:
                r = http.post(f"{API_BASE}/tracking/ping/batch", timeout=10,
                              json={"pings": [{k: v for k, v in p.items() if k != "_tag"} for p in pings]}).json()
                sent += r.get("accepted", 0)
                for rej in r.get("rejected", []):
                    print(f"[virtual_driver] 거절됨 {rej['order_id']}: {rej['detail']}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[virtual_driver] 핑 전송 오류: {e}", flush=True)
        for oid in set(drivers) - alive:  # 하차 완료된 운행은 단말 종료
            drivers.pop(oid, None)
        if sent and sent % 150 < len(runs):
            print(f"[virtual_driver] 운행 {len(runs)}대 · 누적 핑 {sent:,}건", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
