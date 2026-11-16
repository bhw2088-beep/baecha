"""
감사 로그 테이블 (SHA-256 Append-Only 해시 체인 · 디지털 공증)

FSM 상태 전이 · 청구 · 결제 · 환불 · 관제 조치가 한 줄씩 쌓인다. 추가만 하고 고치지 않는다.
각 줄은 직전 줄의 current_hash 를 previous_hash 로 이어 붙여 SHA-256 으로 다시 해시한다.
  current_hash = SHA256(previous_hash | id | 시각 | 화물 | 이벤트 | 이전 → 이후 | 누가 | 금액 | 비고)
→ 중간 한 줄이라도 고치거나 지우면 그 뒤 모든 해시가 어긋나서 '위변조 검사'에 걸린다
  (engines/fsm/order_fsm.py 의 verify_chain).
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class AuditRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    order_id: Mapped[str] = mapped_column(String(30), index=True)
    event: Mapped[str] = mapped_column(String(12))
    actor: Mapped[str] = mapped_column(String(60))
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSONB)
