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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.v1 import api_router
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
    yield
    gps_task.cancel()
    store.persist()  # 종료 직전 마지막 저장


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
    등록 · 수락 등 변경 요청은 바로, 조회 요청은 3초에 한 번(운행 진행률 같은 시뮬레이션 변화)."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        store.persist(throttle=0 if request.method != "GET" else 3)
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
