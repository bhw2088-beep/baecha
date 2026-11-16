"""운임 산정 · 청구 요청 스키마."""

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


class InvoiceIn(BaseModel):
    extra_fee: int = Field(0, ge=0, le=5_000_000)
    toll_fee: int = Field(0, ge=0, le=1_000_000)
    memo: str = Field("", max_length=300)


class PaymentIn(BaseModel):
    """청구서 승인 시 모의 PG 결제 수단 (실제 카드 · 계좌 정보는 받지 않음)."""
    method: Literal["corp_card", "transfer", "virtual"] = "corp_card"


class RefundIn(BaseModel):
    reason: str = Field("관제 결제 취소", min_length=1, max_length=100)
