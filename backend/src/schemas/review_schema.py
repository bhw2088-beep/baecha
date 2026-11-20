"""상호 리뷰 · 블랙리스트 제재 요청 스키마."""

from pydantic import BaseModel, Field


class ReviewIn(BaseModel):
    """운송 완료 화물 평가 (화주 → 기사 · 기사 → 화주)."""
    order_id: str
    stars: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=6)
    comment: str = Field("", max_length=200)


class BlacklistIn(BaseModel):
    """관제 이용 제한(on=true) · 해제(on=false). 사유 필수."""
    on: bool
    reason: str = Field(min_length=1, max_length=200)
