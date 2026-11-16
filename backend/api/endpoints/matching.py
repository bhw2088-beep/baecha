"""매칭 API: 기사 배차 제안 수락/거절 · AI 추천 리스트 · 관제 매칭 실패 조치 · 매칭 분석/설정."""

from fastapi import APIRouter, Depends

from ...core.database import store
from ...core.security import require
from ...schemas.order_schema import AssignIn, IncentiveIn, MatchSettingsIn

router = APIRouter(tags=["matching"])
Driver = Depends(require("driver"))
Admin = Depends(require("admin"))


# ------------------------------------------------------------------ 차주
@router.get("/driver/offer")
async def offer(user: dict = Driver):
    """나에게 온 배차 제안 (없으면 null). 자동 매칭이 '적재 가능 + 가까운' 기사에게 한 건씩 보낸다."""
    return store.driver_offer(user["org_id"])


@router.get("/driver/offers/history")
async def offer_history(user: dict = Driver):
    return store.driver_offer_history(user["org_id"])


@router.post("/driver/offer/{order_id}/accept")
async def accept(order_id: str, user: dict = Driver):
    return store.accept_offer(user["org_id"], order_id)


@router.post("/driver/offer/{order_id}/decline")
async def decline(order_id: str, user: dict = Driver):
    return store.decline_offer(user["org_id"], order_id)


@router.get("/driver/recommendations")
async def recommendations(user: dict = Driver):
    """AI 추천 리스트: 내 차량에 전량 실리는 매칭 대기 화물 (매칭 점수 순)."""
    return store.driver_recommendations(user["org_id"])


@router.post("/driver/recommendations/{order_id}/accept")
async def accept_recommendation(order_id: str, user: dict = Driver):
    return store.accept_pick(user["org_id"], order_id)


# ------------------------------------------------------------------ 관제
@router.get("/admin/offers", dependencies=[Admin])
async def pending_offers():
    """기사 응답을 기다리는 배차 제안 (남은 시간 짧은 순)."""
    return store.pending_offers()


@router.get("/admin/failed-matches", dependencies=[Admin])
async def failed_matches():
    return store.failed_matches()


@router.get("/admin/failed-matches/{order_id}/candidates", dependencies=[Admin])
async def candidates(order_id: str):
    return store.candidates(order_id)[:5]


@router.post("/admin/failed-matches/{order_id}/incentive", dependencies=[Admin])
async def incentive(order_id: str, body: IncentiveIn):
    return store.add_incentive(order_id, body.amount)


@router.post("/admin/failed-matches/{order_id}/rematch", dependencies=[Admin])
async def rematch(order_id: str):
    return store.rematch(order_id)


@router.post("/admin/failed-matches/{order_id}/assign", dependencies=[Admin])
async def assign(order_id: str, body: AssignIn):
    return store.force_assign(order_id, body.driver_id)


@router.get("/admin/matching", dependencies=[Admin])
async def matching_stats():
    return store.matching_stats()


@router.get("/admin/matching/settings", dependencies=[Admin])
async def matching_settings():
    return store.settings_view()


@router.put("/admin/matching/settings")
async def update_matching_settings(body: MatchSettingsIn, user: dict = Admin):
    """탐색 반경 · 수락 제한시간 · 점수 가중치 변경 → 바로 매칭 대기 화물에 다시 적용."""
    return store.update_settings(body.model_dump(), by=user["name"])
