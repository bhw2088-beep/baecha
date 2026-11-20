"""
서버 성능 기록 — '가끔 몇 초씩 멈추는' 원인을 찾기 위한 가벼운 측정 (GET /admin/perf 로 조회).

  save     DB 저장(write-behind) 1회: 전체 시간 = 행 만들기 + 바뀐 행 비교(md5) + DB 쓰기(upsert · commit), 바뀐 행 수
  stall    이벤트 루프 멈춤: 0.1초마다 깨어나는 작업이 300ms 넘게 늦게 깨면 기록 (그동안 다른 요청도 못 받음)
  request  1초 넘게 걸린 API 요청 (그 요청 끝의 저장 시간 포함)
  gc       파이썬 메모리 정리(GC)가 20ms 이상 걸린 경우

시각(t)은 epoch 초 → 부하 시뮬레이터(scripts/simulator.py)가 자기 단계(가입 · 주문 · 매칭 · GPS)별로 나눠서 본다.
기록은 메모리에만, 최근 것만 남긴다 (서버를 다시 켜면 초기화).
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections import deque

STALL_MS = 300          # 이 이상 늦게 깨면 '멈춤'
SLOW_REQUEST_MS = 1000  # 이 이상 걸린 요청만 기록
GC_MS = 20              # 이 이상 걸린 GC 만 기록

saves: deque[dict] = deque(maxlen=5000)     # 저장 1회마다
events: deque[dict] = deque(maxlen=2000)    # stall · request · gc


def record_save(total_ms: float, rows_ms: float, diff_ms: float, db_ms: float, changed: dict[str, int]) -> None:
    saves.append({"t": round(time.time(), 3), "ms": round(total_ms, 1), "rows_ms": round(rows_ms, 1),
                  "diff_ms": round(diff_ms, 1), "db_ms": round(db_ms, 1),
                  "changed": {k: v for k, v in changed.items() if v}})


def record(kind: str, ms: float, **info) -> None:
    events.append({"t": round(time.time(), 3), "kind": kind, "ms": round(ms, 1), **info})


# ---- GC: 시작 · 끝 콜백으로 걸린 시간
_gc_start = [0.0]


def _gc_callback(phase: str, info: dict) -> None:
    if phase == "start":
        _gc_start[0] = time.perf_counter()
    elif _gc_start[0]:
        ms = (time.perf_counter() - _gc_start[0]) * 1000
        if ms >= GC_MS:
            record("gc", ms, generation=info.get("generation"), collected=info.get("collected"))


def install_gc_hook() -> None:
    if _gc_callback not in gc.callbacks:
        gc.callbacks.append(_gc_callback)


# ---- 이벤트 루프 멈춤 감지
async def loop_monitor(interval: float = 0.1) -> None:
    """lifespan 에서 asyncio 작업으로 시작. interval 마다 깨어나서, 늦게 깬 만큼이 '루프가 막혀 있던 시간'."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag = (time.perf_counter() - t0 - interval) * 1000
        if lag >= STALL_MS:
            # 막힌 구간의 시작 시각으로 기록 (지금 - 늦은 만큼)
            events.append({"t": round(time.time() - lag / 1000, 3), "kind": "stall", "ms": round(lag, 1)})


def snapshot(since: float | None = None) -> dict:
    """since(epoch 초) 이후 기록. 저장은 요약 + 느린 저장(200ms 이상) 목록."""
    sv = [s for s in saves if since is None or s["t"] >= since]
    ev = [e for e in events if since is None or e["t"] >= since]
    ms = sorted(s["ms"] for s in sv)
    p = (lambda q: ms[min(len(ms) - 1, int(round(q * (len(ms) - 1))))] if ms else None)
    return {
        "saves": {"count": len(sv), "avg_ms": round(sum(ms) / len(ms), 1) if ms else None, "p95_ms": p(0.95),
                  "max_ms": ms[-1] if ms else None},
        "save_log": sv[-2000:],
        "events": ev,
        "thresholds": {"stall_ms": STALL_MS, "slow_request_ms": SLOW_REQUEST_MS, "gc_ms": GC_MS},
    }
