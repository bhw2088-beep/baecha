"""
Phase 3: Redis Streams 기반 실시간 GPS 수신 파이프라인 (백엔드 안에서 도는 백그라운드 작업)

  가상 기사(simulator/virtual_driver.py) ──XADD──▶ Redis Stream "gps:stream"
                                                        │  소비자 그룹 "backend" (XREADGROUP → 처리 → XACK)
                                                        ▼
                                 gps_stream_worker  ──▶  store.apply_gps()  → 관제 지도 · ETA · 진행률

- 소비자 그룹을 쓰므로 백엔드가 잠깐 꺼져 있어도 메시지가 Redis 에 남아 있다가 다시 켜지면 이어서 처리된다.
- Redis 대기(블로킹 읽기)는 스레드에서, 저장소 반영은 이벤트 루프에서 한다 → API 요청과 동시에 데이터를 건드리지 않음.
- Redis 가 없으면(로컬 메모리 실행) 시작하지 않고, 백엔드 내부 시뮬레이션이 위치를 움직인다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from src.utils.redis_client import available, get_redis

STREAM = "gps:stream"
GROUP = "backend"
CONSUMER = "api-1"
MAXLEN = 20_000  # 스트림 최대 길이 (시뮬레이터가 XADD 할 때 대략 이만큼만 유지)

log = logging.getLogger("uvicorn.error")
stats = {"running": False, "processed": 0, "ignored": 0, "errors": 0, "last_error": None}
# 관제 반영 지연 (KPI): Redis 메시지 ID 앞부분 = XADD 시각(ms, API 가 핑을 받은 직후) → 저장소 반영 시각까지
_lags_ms: deque[float] = deque(maxlen=2000)


def _lag_ms(msg_id) -> float | None:
    try:
        sid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        return max(0.0, time.time() * 1000 - int(sid.split("-")[0]))
    except (ValueError, AttributeError):
        return None


def relay_latency() -> dict:
    """GPS 핑 수신 → 관제 데이터 반영 지연 (최근 2,000건). Redis 가 없으면 count 0."""
    lags = sorted(_lags_ms)
    if not lags:
        return {"count": 0, "p95_ms": None, "avg_ms": None, "running": stats["running"]}
    p95 = lags[min(len(lags) - 1, int(round(0.95 * (len(lags) - 1))))]
    return {"count": len(lags), "p95_ms": round(p95, 1), "avg_ms": round(sum(lags) / len(lags), 1),
            "running": stats["running"]}


def ensure_group():
    r = get_redis()
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:  # noqa: BLE001 — 이미 있으면 BUSYGROUP
        if "BUSYGROUP" not in str(e):
            raise


def _read(block_ms: int = 1000, count: int = 200):
    return get_redis().xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=count, block=block_ms)


async def run(store):
    """lifespan 에서 asyncio 작업으로 시작한다. 취소되면 종료."""
    if not available():
        log.info("GPS   : Redis 없음 → 내부 시뮬레이션으로 위치 갱신")
        return
    await asyncio.to_thread(ensure_group)
    stats["running"] = True
    log.info("GPS   : Redis Streams '%s' 소비 시작 (그룹 %s)", STREAM, GROUP)
    try:
        while True:
            try:
                batches = await asyncio.to_thread(_read)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — Redis 재시작 등: 잠시 쉬고 다시
                stats["errors"] += 1
                stats["last_error"] = str(e)[:160]
                await asyncio.sleep(2)
                try:
                    await asyncio.to_thread(ensure_group)
                except Exception:  # noqa: BLE001
                    pass
                continue
            ids = []
            for _stream, messages in batches or []:
                for msg_id, fields in messages:
                    ids.append(msg_id)
                    try:
                        if store.apply_gps(fields):
                            stats["processed"] += 1
                            lag = _lag_ms(msg_id)
                            if lag is not None and lag < 60_000:  # 백엔드 재시작 동안 쌓인 밀린 메시지는 제외
                                _lags_ms.append(lag)
                        else:
                            stats["ignored"] += 1
                    except Exception as e:  # noqa: BLE001 — 잘못된 메시지는 건너뛴다
                        stats["errors"] += 1
                        stats["last_error"] = str(e)[:160]
            if ids:
                await asyncio.to_thread(get_redis().xack, STREAM, GROUP, *ids)
    finally:
        stats["running"] = False
