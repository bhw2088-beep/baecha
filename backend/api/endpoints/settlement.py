"""정산 API: AI 적정 운임 · 화주 정산/운송 이력(엑셀) · 청구 승인 · 차주 수익/청구."""

import io
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ...core.database import store
from ...core.errors import ApiError
from ...core.security import current_user, require
from ...engines.matcher import spatial as geo
from ...schemas.settlement_schema import EstimateIn, InvoiceIn, PaymentIn, RefundIn
from ...services import billing_service as pricing

router = APIRouter(tags=["settlement"])
Shipper = Depends(require("shipper"))
Driver = Depends(require("driver"))


@router.post("/pricing/estimate")
async def estimate(body: EstimateIn, _: dict = Depends(current_user)):
    if body.truck_type not in pricing.TRUCK_TYPES:
        raise ApiError(400, "지원하지 않는 차종입니다.")
    if not (geo.is_known(body.origin) and geo.is_known(body.destination)):
        raise ApiError(400, "상차지 · 하차지를 지역 목록에서 선택해 주세요.")
    cold = body.cold_chain or body.truck_type in pricing.COLD_TYPES
    return pricing.estimate(body.truck_type, body.origin, body.destination,
                            cold_chain=cold, urgent=body.urgent, wait_minutes=body.wait_minutes,
                            weight_kg=body.weight_kg)


# ------------------------------------------------------------------ 화주
@router.get("/shipper/history")
async def history(q: str = "", status: str = "ALL", period: str = "ALL", user: dict = Shipper):
    return store.shipper_history(user["org_id"], q, status, period)


@router.get("/shipper/history/export")
async def export_history(q: str = "", status: str = "ALL", period: str = "ALL", user: dict = Shipper):
    items = store.shipper_history(user["org_id"], q, status, period)["items"]
    df = pd.DataFrame([{
        "화물 ID": i["id"], "완료일시": i["delivered_at"], "화물명": i["title"], "차종": i["truck_type"],
        "상차지": i["origin"], "하차지": i["destination"],
        "차주": i["driver"]["name"] if i["driver"] else "", "차량번호": i["driver"]["plate"] if i["driver"] else "",
        "기본 운임": i["fare"], "추가금": i["extra_fee"], "통행료": i["toll_fee"], "합계(VAT별도)": i["total_fee"],
        "상태": i["status_label"], "청구 상태": i["billing_label"],
        "상차 거리(km)": i["match"]["pickup_km"] if i["match"] else "",
        "부피 적재율(%)": round(i["match"]["volume_util"] * 100, 1) if i["match"] and i["match"]["volume_util"] else "",
        "인수증 해시": i["pod"]["hash"] if i["pod"] else "",
    } for i in items])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="운송이력")
    buf.seek(0)
    filename = f"baecha_history_{datetime.now():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/shipper/history/{order_id}/approve-invoice")
async def approve_invoice(order_id: str, body: PaymentIn | None = None, user: dict = Shipper):
    """청구서 승인 → 모의 PG 결제 승인 → 정산 완료. body 없으면 법인카드(모의)."""
    return store.approve_invoice(user["org_id"], order_id, (body or PaymentIn()).method)


@router.get("/shipper/invoices")
async def monthly_invoice(period: str | None = None, user: dict = Shipper):
    """월별 인보이스 (그 달 결제 승인분 · 공급가액 · 부가세 · 합계)."""
    return store.monthly_invoice(user["org_id"], period)


# ------------------------------------------------------------------ 관제 (결제 내역 · 취소)
Admin = Depends(require("admin"))


@router.get("/admin/payments", dependencies=[Admin])
async def payments():
    return store.payments()


@router.post("/admin/payments/{order_id}/refund", dependencies=[Admin])
async def refund(order_id: str, body: RefundIn):
    """결제 취소(모의 PG) → 청구 REFUNDED · 화물 DELIVERED → 차주가 다시 청구할 수 있음."""
    return store.refund_payment(order_id, body.reason)


# ------------------------------------------------------------------ 차주
@router.get("/driver/completed")
async def driver_completed(user: dict = Driver):
    return store.driver_completed(user["org_id"])


@router.post("/driver/completed/{order_id}/invoice")
async def request_invoice(order_id: str, body: InvoiceIn, user: dict = Driver):
    return store.request_invoice(user["org_id"], order_id, body.extra_fee, body.toll_fee, body.memo)
