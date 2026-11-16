"""
Phase 6: 가상 기사 GPS 시뮬레이터 (모의 데이터)

운행 중인 화물마다 '가상 기사 단말'이 도로 경로를 따라 달리며 GPS 를 Redis Streams 로 보낸다.
실제 차량 GPS 는 쓰지 않는다 — 모든 위치는 이 시뮬레이터가 만든 모의 데이터다.

  1) 2초마다 백엔드 내부 API(/internal/runs)에서 운행 목록(도로 경로 · 진행률 · 정체 여부)을 받는다
  2) 지난 시간 × 배속(TIME_SCALE) × 속도만큼 경로 위에서 앞으로 이동 (속도는 60~98km/h 사이에서 흔들림)
  3) XADD gps:stream {order_id, driver_id, lat, lon, speed, progress, ts}

실행: docker compose 의 simulator 서비스가 자동 실행
     로컬: python -m simulator.virtual_driver   (API_BASE · REDIS_URL · SIM_API_KEY 환경변수)
"""

from __future__ import annotations

import math
import os
import random
import sys
import time
from datetime import datetime

import redis
import requests

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SIM_API_KEY = os.getenv("SIM_API_KEY", "sim-dev-key")
TIME_SCALE = int(os.getenv("TIME_SCALE", "20"))
INTERVAL = float(os.getenv("SIM_INTERVAL", "2"))
STREAM, MAXLEN = "gps:stream", 20_000


def _km(a, b) -> float:
    """[lon, lat] 두 점 사이 거리(km)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def point_along(route: list[list[float]], ratio: float) -> tuple[float, float]:
    """도로 경로([[lon, lat], ...])의 ratio(0~1) 지점 → (lat, lon)."""
    if len(route) < 2:
        return route[0][1], route[0][0]
    segs = [_km(route[i], route[i + 1]) for i in range(len(route) - 1)]
    target = sum(segs) * max(0.0, min(1.0, ratio))
    for i, s in enumerate(segs):
        if target <= s or i == len(segs) - 1:
            t = 0 if s == 0 else min(1.0, target / s)
            lon = route[i][0] + (route[i + 1][0] - route[i][0]) * t
            lat = route[i][1] + (route[i + 1][1] - route[i][1]) * t
            return lat, lon
        target -= s
    return route[-1][1], route[-1][0]


class VirtualDriver:
    """운행 한 건을 맡은 가상 기사 단말."""

    def __init__(self, run: dict, rng: random.Random):
        self.order_id, self.driver_id = run["order_id"], run["driver_id"]
        self.progress = run["progress"]
        self.speed = run["speed"] or rng.randint(70, 90)
        self.rng = rng

    def step(self, run: dict, dt: float) -> dict:
        self.progress = max(self.progress, run["progress"])  # 서버 쪽 값이 더 앞서 있으면 따라간다
        if run["stopped"]:
            self.speed = 0  # 관제가 '장시간 정체'로 잡은 차량은 멈춰 있음
        else:
            self.speed = max(60, min(98, (self.speed or 78) + self.rng.randint(-4, 4)))
            km = dt * TIME_SCALE / 3600 * self.speed
            self.progress = min(97.0, self.progress + km / max(run["distance_km"], 1) * 100)
        lat, lon = point_along(run["route"], self.progress / 100)
        return {"order_id": self.order_id, "driver_id": self.driver_id or "", "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}", "speed": str(self.speed), "progress": f"{self.progress:.3f}",
                "ts": datetime.now().isoformat(timespec="seconds"), "source": "virtual_driver"}


def main():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    rng = random.Random(11)
    drivers: dict[str, VirtualDriver] = {}
    sent, last = 0, time.monotonic()
    print(f"[virtual_driver] API {API_BASE} · Redis {REDIS_URL} · {INTERVAL}s 간격 · 배속 x{TIME_SCALE}", flush=True)
    while True:
        try:
            runs = requests.get(f"{API_BASE}/internal/runs", headers={"X-Sim-Key": SIM_API_KEY}, timeout=5).json()
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
        alive = set()
        pipe = r.pipeline()
        for run in runs:
            alive.add(run["order_id"])
            vd = drivers.setdefault(run["order_id"], VirtualDriver(run, rng))
            pipe.xadd(STREAM, vd.step(run, dt), maxlen=MAXLEN, approximate=True)
        try:
            pipe.execute()
            sent += len(runs)
        except Exception as e:  # noqa: BLE001
            print(f"[virtual_driver] Redis 오류: {e}", flush=True)
        for oid in set(drivers) - alive:  # 하차 완료된 운행은 단말 종료
            drivers.pop(oid, None)
        if sent and sent % 150 < len(runs):
            print(f"[virtual_driver] 운행 {len(runs)}대 · 누적 GPS {sent:,}건 전송", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
