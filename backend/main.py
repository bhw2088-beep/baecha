"""
배차의 민족 API 서버 (FastAPI) — 앱 진입점

실행 (프로젝트 루트에서):
    uvicorn backend.main:app --reload --reload-dir backend --port 8000
API 문서:
    http://localhost:8000/docs
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.v1 import api_router
from .core import perf
from .core.database import db_status, store
from .core.errors import ApiError
from .core.redis_client import redis_status
from .workers import gps_stream_worker

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """서버 시작 시 DB · Redis 연결을 확인해 로그로 남긴다 (실패해도 메모리 저장소로 계속 동작)."""
    db, rd = await run_in_threadpool(db_status), await run_in_threadpool(redis_status)
    log.info("DB    : %s", f"연결됨 · PostgreSQL {db['postgres']} · PostGIS {db['postgis']}" if db["ok"]
             else f"연결 안 됨 ({db['error']})")
    log.info("Redis : %s", f"연결됨 · Redis {rd['version']}" if rd["ok"] else f"연결 안 됨 ({rd['error']})")
    log.info("저장소: %s", store.db_info)
    gps_task = asyncio.create_task(gps_stream_worker.run(store))  # Redis Streams GPS 수신
    flush_task = asyncio.create_task(_flush_loop())               # 묶어서 미룬 변경을 1초 안에 저장
    perf.install_gc_hook()                                        # 성능 기록: GC · 이벤트 루프 멈춤 (GET /admin/perf)
    stall_task = asyncio.create_task(perf.loop_monitor())
    yield
    gps_task.cancel()
    flush_task.cancel()
    stall_task.cancel()
    store.persist()  # 종료 직전 마지막 저장


# ---- DB 저장 주기 (메모리 작업본 → PostgreSQL write-behind)
# 예전: 변경 요청(POST 등)마다 메모리 데이터 전체를 바로 저장 → 저장하는 동안 다른 요청이 줄을 섬
#       (스모크 테스트: 동시 가입 5건이 모두 약 2.3초, GPS 핑 일부 2.1초)
# 지금: 변경 요청은 WRITE_FLUSH_SEC 안의 것들을 한 번에 모아 저장하고, 건너뛴 변경은 _flush_loop 가 곧바로 저장
#       → 변경은 늦어도 약 1초 안에 DB 에 들어간다. GPS 핑은 Redis Streams 로 가므로 조회처럼 3초에 한 번.
WRITE_FLUSH_SEC = 0.5
READ_FLUSH_SEC = 3.0
_unsaved = {"write": False, "ping": False}   # 저장을 건너뛴 변경 요청 · GPS 핑이 있는가 (묶음 대기)


def _is_gps_ping(path: str) -> bool:
    return "/tracking/ping" in path   # /tracking/ping · /tracking/ping/batch


async def _flush_loop():
    """묶음 대기 중인 변경을 저장 (요청이 끊겨도 변경이 메모리에만 남지 않게).
    변경 요청은 0.5초마다, GPS 핑(재위탁 의심 기록 등)은 3초마다 확인."""
    ticks = 0
    while True:
        await asyncio.sleep(WRITE_FLUSH_SEC)
        ticks += 1
        due_ping = _unsaved["ping"] and ticks % int(READ_FLUSH_SEC / WRITE_FLUSH_SEC) == 0
        if _unsaved["write"] or due_ping:
            _unsaved["write"] = False
            if due_ping:
                _unsaved["ping"] = False
            store.persist()


app = FastAPI(
    title="배차의 민족 API",
    version="1.0.0",
    description="B2B 화물 배차 · 실시간 관제 플랫폼 API (화주 / 차주 / 통합관제)",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@app.middleware("http")
async def persist_after_request(request: Request, call_next):
    """요청이 끝나면 바뀐 데이터를 PostgreSQL 에 저장.
    등록 · 수락 등 변경 요청은 0.5초 단위로 묶어서(늦어도 약 1초 안), 조회 · GPS 핑은 3초에 한 번."""
    t0 = time.perf_counter()
    response = await call_next(request)
    handled_ms = (time.perf_counter() - t0) * 1000
    path = request.url.path
    if path.startswith("/api/"):
        if request.method == "GET":
            store.persist(throttle=READ_FLUSH_SEC)
        elif _is_gps_ping(path):
            if not store.persist(throttle=READ_FLUSH_SEC):
                _unsaved["ping"] = True
        elif not store.persist(throttle=WRITE_FLUSH_SEC):
            _unsaved["write"] = True     # 방금 저장했으면 이번 변경은 _flush_loop 가 곧 저장
        total_ms = (time.perf_counter() - t0) * 1000
        if total_ms >= perf.SLOW_REQUEST_MS:   # 느린 요청: 처리 시간과 끝의 저장 시간을 나눠 기록
            perf.record("request", total_ms, method=request.method, path=path,
                        handler_ms=round(handled_ms, 1), persist_ms=round(total_ms - handled_ms, 1))
    return response


app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


# 차주용 모바일 웹 (Three.js 3D 적재 가이드): http://localhost:8000/m/  (휴대폰: http://<PC IP>:8000/m/)
MOBILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mobile_web")


@app.get("/m", include_in_schema=False)
async def mobile_redirect():
    return RedirectResponse("/m/")


if os.path.isdir(MOBILE_DIR):
    app.mount("/m", StaticFiles(directory=MOBILE_DIR, html=True), name="mobile_web")
