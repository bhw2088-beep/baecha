"""
정산 서비스: AI 적정 운임 산출(규칙 기반) · 차주 청구 · 화주 승인.

[AI 적정 운임] (원래 pricing.py)

원본 시뮬레이터의 '요인별 가중치' 설명을 그대로 계산식으로 옮겼다.
  기본 운임  = max(차종별 최저 운임, 차종별 km 단가 × 도로거리)
  + 적재 중량(차량 최대 적재량 대비) / 기상 상태(날씨) / 긴급성 / 현장 대기시간 / 작업·적재 조건
나중에 ML 모델로 교체할 때는 estimate() 의 반환 형식만 유지하면 된다.
"""

import hashlib
from datetime import datetime

from ..core.errors import ApiError
from ..engines.matcher import spatial as geo

# 차종: (최저 운임, km 단가)
TRUCK_TYPES: dict[str, tuple[int, int]] = {
    "1톤 카고": (120_000, 1_600),
    "5톤 윙바디": (250_000, 2_300),
    "5톤 냉동탑차": (280_000, 2_500),
    "11톤 탑차": (360_000, 2_800),
    "11톤 냉동탑차": (380_000, 3_000),
    "25톤 무진동": (600_000, 4_200),
}

COLD_TYPES = {"5톤 냉동탑차", "11톤 냉동탑차"}


def current_weather(now: datetime | None = None) -> tuple[str, int]:
    """기상청 API 연동 전까지는 시간대 기반으로 흉내만 낸다."""
    now = now or datetime.now()
    if now.hour in (6, 7, 8, 17, 18, 19):
        return "강우 · 서행", 15_000
    if now.hour >= 22 or now.hour < 5:
        return "야간 · 시정 저하", 10_000
    return "맑음", 0


def _round5k(value: float) -> int:
    return int(round(value / 5_000) * 5_000)


def weight_surcharge(base: int, truck_type: str, weight_kg: float | None) -> tuple[int, str]:
    """적재 중량 할증: 차량 최대 적재량의 50% 까지 0원, 100% 에서 기본 운임의 +10% (그 사이는 비례)."""
    from ..engines.packer.trucks import TRUCKS
    truck = TRUCKS.get(truck_type)
    if not weight_kg or not truck:
        return 0, "중량 정보 없음"
    ratio = weight_kg / truck.max_weight
    amount = _round5k(base * 0.10 * max(0.0, min(1.0, (ratio - 0.5) / 0.5)))
    return amount, f"{weight_kg:,.0f}kg · 최대 적재량의 {ratio * 100:.0f}%"


def estimate(truck_type: str, origin: str, destination: str, *,
             cold_chain: bool = False, urgent: bool = False, wait_minutes: int = 30,
             weight_kg: float | None = None) -> dict:
    """거리(도로) · 차종 · 중량 · 조건으로 적정 운임을 계산한다."""
    min_fare, per_km = TRUCK_TYPES.get(truck_type, TRUCK_TYPES["11톤 탑차"])
    distance = geo.road_km(origin, destination)
    base = _round5k(max(min_fare, per_km * distance))
    weight_fee, weight_detail = weight_surcharge(base, truck_type, weight_kg)

    weather_label, weather_fee = current_weather()
    factors = [
        {"key": "weight", "label": "적재 중량", "detail": weight_detail, "amount": weight_fee},
        {"key": "weather", "label": "기상 상태", "detail": weather_label, "amount": weather_fee},
        {"key": "urgent", "label": "긴급성", "detail": "당일 특송" if urgent else "일반", "amount": 20_000 if urgent else 0},
        {"key": "wait", "label": "현장 대기시간", "detail": f"예상 {wait_minutes}분",
         "amount": (wait_minutes // 30) * 10_000},
        {"key": "work", "label": "작업·적재 조건", "detail": "콜드체인 온도 관리" if cold_chain else "일반 적재",
         "amount": 30_000 if cold_chain else 0},
    ]
    total = base + sum(f["amount"] for f in factors)
    return {
        "truck_type": truck_type,
        "distance_km": distance,
        "per_km": per_km, "min_fare": min_fare,
        "weight_kg": weight_kg,
        "base": base,
        "factors": factors,
        "recommended": _round5k(total),
        "band": [_round5k(total * 0.93), _round5k(total * 1.08)],
    }


def price_gap_ratio(offered: int, recommended: int) -> float:
    """희망 운임이 추천가보다 얼마나 낮은지 (양수 = 저렴)."""
    if recommended <= 0:
        return 0.0
    return (recommended - offered) / recommended


# ------------------------------------------------------------------ 모의 PG (결제대행) — 실제 결제 없음
PAY_METHODS = {"corp_card": "법인카드 (모의)", "transfer": "계좌이체 (모의)", "virtual": "가상계좌 (모의)"}
VAT_RATE = 0.10


class MockPG:
    """
    모의 PG: 실제 결제사 · 카드 · 계좌 정보를 쓰지 않고, 승인 → 취소 흐름만 재현한다.
    거래번호(tid) · 승인번호는 화물 번호와 시각으로 만든 가짜 값이고, 응답에 test=True 를 붙인다.
    실제 PG 를 붙일 때는 approve() / cancel() 의 반환 형식만 맞추면 된다.
    """
    NAME = "MockPG"

    @staticmethod
    def approve(order_id: str, amount: int, method: str, at: datetime | None = None) -> dict:
        if method not in PAY_METHODS:
            raise ApiError(400, "지원하지 않는 결제 수단입니다.")
        if amount <= 0:
            raise ApiError(400, "결제 금액이 올바르지 않습니다.")
        at = at or datetime.now()
        digest = hashlib.sha256(f"{order_id}|{amount}|{at.isoformat()}".encode()).hexdigest()
        return {"pg": MockPG.NAME, "test": True, "tid": f"MOCK-{at:%Y%m%d%H%M%S}-{digest[:6].upper()}",
                "approval_no": str(int(digest[6:14], 16) % 10**8).zfill(8), "method": method,
                "method_label": PAY_METHODS[method], "amount": amount, "supply": round(amount / (1 + VAT_RATE)),
                "vat": amount - round(amount / (1 + VAT_RATE)), "status": "APPROVED",
                "approved_at": at.isoformat(timespec="seconds"), "cancelled_at": None, "cancel_reason": None}

    @staticmethod
    def cancel(payment: dict, reason: str) -> dict:
        if not payment or payment.get("status") != "APPROVED":
            raise ApiError(409, "취소할 수 있는 결제가 없습니다.")
        return {**payment, "status": "CANCELLED", "cancel_reason": reason or "관제 취소",
                "cancelled_at": datetime.now().isoformat(timespec="seconds")}


class BillingMixin:
    """차주 → 화주 청구 · 화주 결제 승인(모의 PG) · 결제 취소/환불 · 월별 인보이스 (Store 에 섞어 쓰는 Mixin)."""

    @staticmethod
    def _fare_est(o: dict) -> dict:
        """AI 적정 운임 (도로거리 · 차종 · 적재 중량 · 조건). 등록 시점 기준으로 한 번 계산해 화물에 저장한다."""
        est = o.get("fare_est")
        if est is None:
            w = o["packing"]["metrics"]["loaded_weight"] if o["packing"] else None
            e = estimate(o["truck_type"], o["origin"], o["destination"], cold_chain=o["cold_chain"],
                         urgent=o["urgent"], weight_kg=w)
            est = o["fare_est"] = {k: e[k] for k in ("recommended", "band", "base", "distance_km", "per_km",
                                                     "weight_kg")} | {"factors": [f for f in e["factors"] if f["amount"]]}
        fare = o["price"] + o["incentive"]
        return {**est, "fare": fare, "gap_ratio": round((fare - est["recommended"]) / est["recommended"], 3),
                "fare_per_km": round(fare / max(o["distance_km"], 1))}

    @staticmethod
    def _total(o: dict) -> int:
        return o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]

    def approve_invoice(self, shipper_id: str, order_id: str, method: str = "corp_card") -> dict:
        """화주 청구서 승인 → 모의 PG 결제 승인 → FSM pay (청구 PAID · 화물 SETTLED)."""
        o = self._get_order(order_id)
        if o["shipper_id"] != shipper_id:
            raise ApiError(403, "이 화물에 접근할 권한이 없습니다.")
        if o["billing"] != "REQUESTED":
            raise ApiError(400, "승인 대기 중인 청구서가 없습니다.")
        total = self._total(o)
        payment = MockPG.approve(o["id"], total, method)
        self._fsm(o, "pay", self._actor("shipper", shipper_id), amount=total,
                  note=f"모의 PG 승인 {payment['tid']} · {payment['method_label']} · 승인번호 {payment['approval_no']}")
        o["payment"] = payment
        o.setdefault("payments", []).append(payment)
        if o["requested_at"]:
            o["paid_days"] = round((datetime.now() - o["requested_at"]).total_seconds() / 86400, 1)
        return self.order_view(o)

    def refund_payment(self, order_id: str, reason: str, actor: str = "관제") -> dict:
        """결제 취소(환불) → 모의 PG 취소 → FSM refund (청구 REFUNDED · 화물 DELIVERED). 차주는 다시 청구할 수 있다."""
        o = self._get_order(order_id)
        if o["billing"] != "PAID":
            raise ApiError(409, "결제 완료된 건만 취소할 수 있습니다.")
        cancelled = MockPG.cancel(o.get("payment"), reason)
        self._fsm(o, "refund", actor, amount=cancelled["amount"],
                  note=f"모의 PG 취소 {cancelled['tid']} · 사유: {cancelled['cancel_reason']}")
        o["payment"] = cancelled
        o["payments"] = [cancelled if p["tid"] == cancelled["tid"] else p for p in o.get("payments", [])]
        return self.order_view(o)

    def request_invoice(self, driver_id: str, order_id: str, extra_fee: int, toll_fee: int, memo: str) -> dict:
        o = self._get_order(order_id)
        if o["driver_id"] != driver_id:
            raise ApiError(403, "내 운송 건이 아닙니다.")
        if o["billing"] not in ("UNBILLED", "REFUNDED"):
            raise ApiError(409, "이미 청구서를 발송한 건입니다.")
        again = o["billing"] == "REFUNDED"
        o.update(extra_fee=extra_fee, toll_fee=toll_fee, memo=memo.strip(), requested_at=datetime.now())
        total = self._total(o)
        self._fsm(o, "request", self._actor("driver", driver_id), amount=total,
                  note=("재청구 · " if again else "")
                  + f"기본 {o['price'] + o['incentive']:,} + 추가 {extra_fee:,} + 통행료 {toll_fee:,}")
        return self.order_view(o)

    def payments(self, shipper_id: str | None = None) -> list[dict]:
        """결제 내역 (승인 · 취소). 관제는 전체, 화주는 자기 것만."""
        rows = []
        for o in self.orders.values():
            if shipper_id and o["shipper_id"] != shipper_id:
                continue
            for p in o.get("payments", []):
                d = self.drivers.get(o["driver_id"], {})
                rows.append({**p, "order_id": o["id"], "title": o["title"], "billing": o["billing"],
                             "shipper": self.shippers.get(o["shipper_id"], {}).get("name", "-"),
                             "driver": d.get("name", "-")})
        return sorted(rows, key=lambda p: p["approved_at"], reverse=True)

    def monthly_invoice(self, shipper_id: str, period: str | None = None) -> dict:
        """월별 인보이스: 그 달에 결제 승인된 운송비 (공급가액 · 부가세 10% · 합계). 취소된 결제는 제외."""
        paid = [p for p in self.payments(shipper_id) if p["status"] == "APPROVED"]
        periods = sorted({p["approved_at"][:7] for p in paid}, reverse=True)
        period = period or (periods[0] if periods else datetime.now().strftime("%Y-%m"))
        lines = [p for p in paid if p["approved_at"][:7] == period]
        total = sum(p["amount"] for p in lines)
        supply = sum(p["supply"] for p in lines)
        s = self.shippers.get(shipper_id, {})
        return {"invoice_no": f"INV-{shipper_id}-{period.replace('-', '')}", "period": period, "periods": periods,
                "shipper": s.get("name", "-"), "biz_no": s.get("biz_no", "-"), "count": len(lines),
                "supply": supply, "vat": total - supply, "total": total, "pg": MockPG.NAME,
                "lines": [{k: p[k] for k in ("order_id", "title", "driver", "approved_at", "method_label", "tid",
                                             "approval_no", "supply", "vat", "amount")} for p in lines]}
