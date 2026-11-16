"""매칭 성능 로그 테이블 (matching_metrics_logs): 헝가리안 배정 라운드마다 지연 · 공차 거리 · 후보 수를 남긴다."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class MatchingMetricRow(Base):
    __tablename__ = "matching_metrics_logs"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)          # MR-<순번>
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    orders: Mapped[int] = mapped_column(Integer)                            # 배정 대상 화물
    assigned: Mapped[int] = mapped_column(Integer)                          # 제안 보낸 쌍
    latency_ms: Mapped[float] = mapped_column(Float)                        # 평가 + 배정 소요 시간
    deadhead_reduction_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 그리디 대비 공차 거리 단축률
    data: Mapped[dict] = mapped_column(JSONB)
