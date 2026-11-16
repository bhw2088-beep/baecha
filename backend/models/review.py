"""화주-기사 상호 리뷰 테이블 (별점 1~5 · 태그형 평가). 기사 평점은 매칭 피처 · 블랙리스트에 쓰인다."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)        # REV-<화물 ID>-<S|D>
    order_id: Mapped[str] = mapped_column(String(30), index=True)
    from_role: Mapped[str] = mapped_column(String(10))                   # shipper · driver
    to_role: Mapped[str] = mapped_column(String(10))
    to_id: Mapped[str] = mapped_column(String(20), index=True)           # 평가 받은 기사 D-xxx / 화주 S-xxx
    stars: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    data: Mapped[dict] = mapped_column(JSONB)                            # tags · comment · from_id
