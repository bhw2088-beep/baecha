"""
Phase 3: Redis Streams 기반 실시간 GPS 수신 파이프라인 (백엔드 안에서 도는 백그라운드 작업)

  가상 기사(simulator/virtual_driver.py) ──XADD──▶ Redis Stream "gps:stream"
                                                        │  소비자 그룹 "backend" (XREADGROUP → 처리 → XACK)
                                                        ▼
                                 gps_stream_worker  ──▶  store.apply_gps()  → 관제 지도 · ETA · 진행률

- 소비자 그룹을 쓰므로 백엔드가 잠깐 꺼져 있어도 메시지가 Redis 에 남아 있다가 다시 켜지면 이어서 처리된다.
- 장애 복구: 메시지를 읽고 XACK 하기 전에 백엔드가 죽으면 그 메시지는 '처리 중(PEL)'으로 남는다
  → 시작할 때 recover_pending() 이 그 메시지부터 다시 처리하고 새 메시지를 읽는다 (tests/checks_6/2_redis_recovery.py).
- Redis 대기(블로킹 읽기)는 스레드에서, 저장소 반영은 이벤트 루프에서 한다 → API 요청과 동시에 데이터를 건드리지 않음.
- Redis 가 없으면(로컬 메모리 실행) 시작하지 않고, 백엔드 내부 시뮬레이션이 위치를 움직인다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from ..core.redis_client import available, get_redis

STREAM = "gps:stream"
GROUP = "backend"
CONSUMER = "api-1"
MAXLEN = 20_000  # 스트림 최대 길이 (시뮬레이터가 XADD 할 때 대략 이만큼만 유지)

log = logging.getLogger("uvicorn.error")
stats = {"running": False, "processed": 0, "ignored": 0, "errors": 0, "last_error": None,
         "recovered": 0,   # 재시작 때 다시 처리한 미처리(XACK 누락) 메시지
         "lost": 0}        # 미처리로 남았지만 스트림 길이 제한으로 이미 지워져 처리 못 한 메시지
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


CLAIM_IDLE_MS = 60_000   # 다른 소비자가 이만큼 넘게 붙잡고 있는 미처리 메시지는 죽은 것으로 보고 가져온다


def _handle(store, messages) -> list:
    """메시지 목록을 저장소에 반영하고 XACK 할 ID 목록을 돌려준다 (새 메시지 · 재처리 공용)."""
    ids = []
    for msg_id, fields in messages:
        ids.append(msg_id)
        if not fields:            # 미처리 상태로 남았지만 스트림 길이 제한(MAXLEN)으로 이미 지워진 메시지
            stats["lost"] += 1
            continue
        try:
            if store.apply_gps(_decode(fields)):
                stats["processed"] += 1
                lag = _lag_ms(msg_id)
                if lag is not None and lag < 60_000:  # 백엔드 재시작 동안 쌓인 밀린 메시지는 제외
                    _lags_ms.append(lag)
            else:
                stats["ignored"] += 1
        except Exception as e:  # noqa: BLE001 — 잘못된 메시지는 건너뛴다
            stats["errors"] += 1
            stats["last_error"] = str(e)[:160]
    return ids


def _decode(fields: dict) -> dict:
    return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in fields.items()}


def recover_pending(store, claim_idle_ms: int = CLAIM_IDLE_MS, count: int = 200) -> int:
    """[장애 복구] 읽었지만 XACK 하지 못한 채 남은 메시지(PEL)를 다시 처리한다. 처리한 개수를 돌려준다.
    - 백엔드가 메시지를 읽고 반영 · XACK 하기 전에 죽으면, 그 메시지는 '처리 중'으로 Redis 에 남고
      새 메시지만 읽는 '>' 로는 다시 오지 않는다 → 시작할 때 이 소비자의 미처리 목록('0')부터 비운다.
    - 다른 소비자(여러 대로 늘렸을 때 죽은 서버)가 claim_idle_ms 넘게 붙잡고 있던 메시지도 XAUTOCLAIM 으로 가져온다.
    새 메시지보다 먼저 처리하므로 위치가 시간 순서대로 반영된다.
    (동기 함수: 테스트 · 도구용. 서버 안에서는 저장소를 이벤트 루프에서만 바꾸도록 recover_pending_async 를 쓴다)"""
    _claim_stale(claim_idle_ms, count)
    r = get_redis()
    done, last = 0, "0"
    while True:      # 이 소비자의 미처리 목록: ID '0' 부터 읽으면 아직 XACK 안 된 메시지만 온다
        messages = _read_pending(last, count)
        if not messages:
            break
        ids = _handle(store, messages)
        r.xack(STREAM, GROUP, *ids)
        done += len(ids)
        last = messages[-1][0]
    stats["recovered"] += done
    return done


async def recover_pending_async(store, claim_idle_ms: int = CLAIM_IDLE_MS, count: int = 200) -> int:
    """recover_pending 과 같지만 Redis 대기는 스레드에서, 저장소 반영(_handle)은 이벤트 루프에서 한다
    → API 요청과 같은 순간에 저장소를 건드리지 않음 (새 메시지 처리와 같은 방식)."""
    await asyncio.to_thread(_claim_stale, claim_idle_ms, count)
    done, last = 0, "0"
    while True:
        messages = await asyncio.to_thread(_read_pending, last, count)
        if not messages:
            break
        ids = _handle(store, messages)
        await asyncio.to_thread(get_redis().xack, STREAM, GROUP, *ids)
        done += len(ids)
        last = messages[-1][0]
    stats["recovered"] += done
    return done


def _read_pending(last, count: int) -> list:
    batches = get_redis().xreadgroup(GROUP, CONSUMER, {STREAM: last}, count=count)
    return [m for _s, ms in batches or [] for m in ms]


def _claim_stale(claim_idle_ms: int, count: int) -> None:
    """죽은 소비자가 claim_idle_ms 넘게 붙잡고 있던 미처리 메시지를 이 소비자 몫으로 옮긴다 (저장소는 안 건드림)."""
    r = get_redis()
    try:
        start = "0-0"
        for _ in range(10_000):   # 죽은 소비자 몫 → 이 소비자 미처리 목록으로 옮김 (응답: [다음 시작 ID, 메시지, 지워진 ID])
            res = r.xautoclaim(STREAM, GROUP, CONSUMER, min_idle_time=claim_idle_ms, start_id=start, count=count)
            start = res[0]
            if len(res) > 2:               # Redis 7: 이미 지워진 메시지는 미처리 목록에서 빼고 ID 만 알려 줌 → 유실로 셈
                stats["lost"] += len(res[2])
            if start in (b"0-0", "0-0"):   # 끝까지 훑음
                break
    except Exception as e:  # noqa: BLE001 — Redis 6.2 미만(XAUTOCLAIM 없음)이면 이 소비자 몫만 처리
        stats["last_error"] = f"XAUTOCLAIM: {str(e)[:120]}"


async def run(store):
    """lifespan 에서 asyncio 작업으로 시작한다. 취소되면 종료."""
    if not available():
        log.info("GPS   : Redis 없음 → 내부 시뮬레이션으로 위치 갱신")
        return
    await asyncio.to_thread(ensure_group)
    stats["running"] = True
    try:
        n = await recover_pending_async(store)
        if n:
            log.info("GPS   : 재시작 전 미처리 메시지 %d건 재처리 (XACK 누락 복구)", n)
    except Exception as e:  # noqa: BLE001 — 복구 실패해도 새 메시지 처리는 계속
        stats["errors"] += 1
        stats["last_error"] = f"recover: {str(e)[:140]}"
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
                ids += _handle(store, messages)
            if ids:
                await asyncio.to_thread(get_redis().xack, STREAM, GROUP, *ids)
    finally:
        stats["running"] = False
