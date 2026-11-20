"""운임 산정 · 청구 요청 스키마."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EstimateIn(BaseModel):
    truck_type: str
    origin: str = Field(min_length=2)
    destination: str = Field(min_length=2)
    cold_chain: bool = False
    urgent: bool = False
    wait_minutes: int = Field(30, ge=0, le=240)
    weight_kg: float | None = Field(None, gt=0, le=60_000)  # 적재 시뮬레이션 총중량 (중량 할증)
    max_item_mm: float | None = Field(None, gt=0, le=20_000)  # 가장 긴 품목 길이 (장재물 할증)
    pickup_at: datetime | None = None                       # 상차 예약 시각 (예약 배차)


class InvoiceIn(BaseModel):
    extra_fee: int = Field(0, ge=0, le=5_000_000)
    toll_fee: int = Field(0, ge=0, le=1_000_000)
    memo: str = Field("", max_length=300)


class PaymentIn(BaseModel):
    """청구서 승인 시 모의 PG 결제 수단 (실제 카드 · 계좌 정보는 받지 않음)."""
    method: Literal["corp_card", "transfer", "virtual"] = "corp_card"


class RefundIn(BaseModel):
    reason: str = Field("관제 결제 취소", min_length=1, max_length=100)


class PayoutIn(BaseModel):
    """기사 수익금 일괄 송금 대상 (비우면 전체)."""
    driver_ids: list[str] = Field(default_factory=list, max_length=500)
