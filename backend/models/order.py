"""
화물(주문) 도메인: 상태 흐름 · 화면 라벨 + DB 테이블(화물 · 이상 감지 · 앱 상태).

화물 상태 흐름
  MATCHING(매칭중) → ASSIGNED(배차완료) → IN_TRANSIT(배송중) → DELIVERED(운송완료) → SETTLED(정산완료)
청구 상태 흐름 (차주 → 화주)
  UNBILLED(청구 필요) → REQUESTED(승인 대기) → PAID(입금 완료)

화물의 상차지 · 하차지는 PostGIS geography(POINT) 로도 저장한다 (공간 검색 · 관제 지도).
자주 검색하는 값은 컬럼으로, 나머지(적재 계획 · 매칭 이력 · 제안 등)는 JSONB(data)로 둔다.
"""

from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

STATUS_LABEL = {
    "MATCHING": "매칭중",
    "ASSIGNED": "배차완료",
    "IN_TRANSIT": "배송중",
    "DELIVERED": "운송완료",
    "SETTLED": "정산완료",
}
BILLING_LABEL = {"UNBILLED": "청구 필요", "REQUESTED": "승인 대기", "PAID": "입금 완료", "REFUNDED": "결제 취소"}
ANOMALY_TYPE_LABEL = {"COLD": "온도 이상", "STAGNATION": "장시간 정체", "DEVIATION": "경로 이탈",
                      "DELAY": "도착 지연", "SUSPICIOUS_REOUTSOURCING": "불법 재위탁 의심"}
MATCH_EVENT_LABEL = {
    "offered": "배차 제안", "accepted": "기사 수락 · 배차 확정", "declined": "기사 거절",
    "expired": "응답 없음 (시간 초과)", "assigned": "관제 직접 배차", "reposted": "보조금 재공고",
    "rematch": "관제 재매칭", "cancelled": "제안 취소", "picked": "기사 직접 수락 (AI 추천 리스트)",
}
MATCH_BY_LABEL = {"auto": "자동 매칭", "pick": "AI 추천 수락", "admin": "관제 직접 지정"}
ACTIVE_RUN = ("ASSIGNED", "IN_TRANSIT")
DONE = ("DELIVERED", "SETTLED")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


# ------------------------------------------------------------------ DB 테이블
class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    shipper_id: Mapped[str] = mapped_column(String(20), index=True)
    driver_id: Mapped[str | None] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(12), index=True)
    title: Mapped[str] = mapped_column(String(80))
    truck_type: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(80))
    destination: Mapped[str] = mapped_column(String(80))
    origin_geom = mapped_column(Geography("POINT", srid=4326))
    dest_geom = mapped_column(Geography("POINT", srid=4326))
    planned_polyline: Mapped[str | None] = mapped_column(Text, nullable=True)  # 계획 경로 (encoded polyline)
    price: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    data: Mapped[dict] = mapped_column(JSONB)


class AnomalyRow(Base):
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(30), index=True)
    driver_id: Mapped[str | None] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(10), index=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    data: Mapped[dict] = mapped_column(JSONB)


from .audit import AuditRow  # noqa: E402 — 감사 로그 테이블은 models/audit.py (해시 체인), 예전 import 경로 유지

__all__ = ["OrderRow", "AnomalyRow", "AuditRow", "AppStateRow"]


class AppStateRow(Base):
    """매칭 설정 · 설정 변경 이력 · 화물 번호 순번 등 앱 전역 상태 (key → JSON)."""
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
