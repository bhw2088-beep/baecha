"""API v1 라우터 통합 (/api/v1/...)."""

from fastapi import APIRouter

from .endpoints import auth, matching, orders, settlement, tracking

API_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_PREFIX)
for r in (auth.router, orders.router, matching.router, tracking.router, settlement.router):
    api_router.include_router(r)
