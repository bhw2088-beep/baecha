"""리뷰 API: 화주-기사 상호 별점 · 태그 평가, 관제 평판 모니터링 · 블랙리스트 제재."""

from fastapi import APIRouter, Depends

from src.orm.database import store
from src.utils.security import current_user, require
from src.schemas.review_schema import BlacklistIn, ReviewIn
from src.services.review_service import BLACKLIST_AVG, BLACKLIST_MIN_REVIEWS, DRIVER_TAGS, SHIPPER_TAGS

router = APIRouter(tags=["reviews"])
Admin = Depends(require("admin"))


@router.get("/reviews/tags")
async def tags():
    """화주 → 기사 / 기사 → 화주 평가 태그 · 블랙리스트 기준."""
    return {"driver": DRIVER_TAGS, "shipper": SHIPPER_TAGS,
            "blacklist_rule": f"리뷰 {BLACKLIST_MIN_REVIEWS}건 이상 & 평균 {BLACKLIST_AVG}점 미만 → 자동 이용 제한"}


@router.get("/reviews/pending")
async def pending(user: dict = Depends(current_user)):
    """내가 아직 평가하지 않은 운송 완료 화물."""
    return store.reviewable(user) if user["role"] in ("shipper", "driver") else []


@router.post("/reviews", status_code=201)
async def create(body: ReviewIn, user: dict = Depends(current_user)):
    return store.create_review(user, body.order_id, body.stars, body.tags, body.comment)


@router.get("/reviews/mine")
async def mine(user: dict = Depends(current_user)):
    """내가 받은 평가 · 평점."""
    role = user["role"]
    if role not in ("shipper", "driver"):
        return {"rating": None, "items": []}
    rating = store.driver_rating(user["org_id"]) if role == "driver" else store.shipper_rating(user["org_id"])
    return {"rating": rating, "items": store.list_reviews(role, user["org_id"])}


# ------------------------------------------------------------------ 관제
@router.get("/admin/reviews", dependencies=[Admin])
async def all_reviews():
    return store.list_reviews()


@router.get("/admin/reputation", dependencies=[Admin])
async def reputation():
    """기사별 평점 · 태그 · 블랙리스트 상태 (제재 대상 먼저)."""
    return store.reputation_board()


@router.post("/admin/drivers/{driver_id}/blacklist")
async def blacklist(driver_id: str, body: BlacklistIn, user: dict = Admin):
    """이용 제한 → 매칭 후보 제외 · 진행 중인 제안 회수. 해제하면 바로 다시 후보."""
    return store.set_blacklist(driver_id, body.on, body.reason, by=user["name"])
