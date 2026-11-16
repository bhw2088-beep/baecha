"""관제 · 추적 API: 기사 운행(상차/하차 + e-POD) · 전국 차량 위치 · 관제 현황 · 이상 감지."""

from fastapi import APIRouter, Depends, Header, HTTPException

from ...config import settings
from ...core.database import store
from ...core.security import require
from ...schemas.order_schema import CompleteIn
from ...workers import gps_stream_worker

router = APIRouter(tags=["tracking"])
Driver = Depends(require("driver"))
Admin = Depends(require("admin"))


# ------------------------------------------------------------------ 차주 운행
@router.get("/driver/profile")
async def driver_profile(user: dict = Driver):
    return store.driver_profile(user["org_id"])


@router.post("/driver/runs/{order_id}/start")
async def start_run(order_id: str, user: dict = Driver):
    return store.start_run(user["org_id"], order_id)


@router.post("/driver/runs/{order_id}/complete")
async def complete_run(order_id: str, body: CompleteIn, user: dict = Driver):
    return store.complete_run(user["org_id"], order_id, body.receiver)


# ------------------------------------------------------------------ 관제
@router.get("/admin/summary", dependencies=[Admin])
async def admin_summary():
    return store.admin_summary()


@router.get("/admin/fleet", dependencies=[Admin])
async def fleet():
    return store.fleet()


@router.get("/admin/anomalies", dependencies=[Admin])
async def anomalies(type: str = "ALL", status: str = "ALL"):
    return store.list_anomalies(type, status)


@router.post("/admin/anomalies/{anomaly_id}/resolve", dependencies=[Admin])
async def resolve(anomaly_id: str):
    return store.resolve_anomaly(anomaly_id)


@router.post("/admin/reset", dependencies=[Admin])
async def reset_demo():
    """[데모 데이터 초기화] DB 를 비우고 시드 데이터로 다시 채운다 (시연 전 · 테스트 후). 데모 계정 로그인은 유지."""
    return store.reset_all()


@router.get("/admin/audit", dependencies=[Admin])
async def audit(order_id: str | None = None, financial: bool = False, limit: int = 200):
    """감사 로그 (FSM 상태 전이 · 청구 · 결제 · 환불). financial=true 면 재무 변경만."""
    return store.audit_log(order_id, financial, min(limit, 1000))


# ------------------------------------------------------------------ 시뮬레이터 전용 (내부)
def _sim_key(x_sim_key: str = Header(default="")):
    if x_sim_key != settings.sim_api_key:
        raise HTTPException(status_code=401, detail="시뮬레이터 키가 올바르지 않습니다.")


@router.get("/internal/runs", dependencies=[Depends(_sim_key)], include_in_schema=False)
async def sim_runs():
    """가상 기사 시뮬레이터가 2초마다 읽는 운행 목록 (도로 경로 · 진행률 · 정체 여부)."""
    return store.sim_runs()


@router.get("/admin/gps", dependencies=[Admin])
async def gps_status():
    """GPS 수신 파이프라인 상태 (Redis Streams 소비 건수 등)."""
    return {**gps_stream_worker.stats, "live": store.admin_summary()["gps_live"]}


@router.get("/admin/agent/briefing", dependencies=[Admin])
async def agent_briefing():
    """규칙 기반 AI 관제 보조: 매칭 실패 · 운행 이상 · 지연 · 정산 · 가입 심사를 점검해 조치와 안내 문구 초안을 제안."""
    from ...engines.ai import rule_agent
    return rule_agent.briefing(store)
