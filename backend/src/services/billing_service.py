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

from src.utils.errors import ApiError
from src.engines import pricing_engine as market_engine
from src.engines.matcher import spatial as geo

# 차종: (최저 운임, km 단가)
TRUCK_TYPES: dict[str, tuple[int, int]] = {
    "1톤 카고": (120_000, 1_600),
    "2.5톤 카고": (180_000, 1_900),
    "5톤 윙바디": (250_000, 2_300),
    "5톤 냉동탑차": (280_000, 2_500),
    "11톤 탑차": (360_000, 2_800),
    "11톤 냉동탑차": (380_000, 3_000),
    "25톤 무진동": (600_000, 4_200),
}

COLD_TYPES = {"5톤 냉동탑차", "11톤 냉동탑차"}


def current_weather(region: str = "대전", now: datetime | None = None) -> dict:
    """기상청 모의 API (engines/pricing_engine.py): 확률 분포 맑음 · 비(+15%) · 폭설(+30%)."""
    return market_engine.mock_get_weather_condition(region, now)


def _round5k(value: float) -> int:
    return int(round(value / 5_000) * 5_000)


def weight_surcharge(base: int, truck_type: str, weight_kg: float | None) -> tuple[int, str]:
    """적재 중량 할증: 차량 최대 적재량의 50% 까지 0원, 100% 에서 기본 운임의 +10% (그 사이는 비례)."""
    from src.engines.packer.trucks import TRUCKS
    truck = TRUCKS.get(truck_type)
    if not weight_kg or not truck:
        return 0, "중량 정보 없음"
    ratio = weight_kg / truck.max_weight
    amount = _round5k(base * 0.10 * max(0.0, min(1.0, (ratio - 0.5) / 0.5)))
    return amount, f"{weight_kg:,.0f}kg · 최대 적재량의 {ratio * 100:.0f}%"


def estimate(truck_type: str, origin: str, destination: str, *,
             cold_chain: bool = False, urgent: bool = False, wait_minutes: int = 30,
             weight_kg: float | None = None, market: dict | None = None,
             max_item_mm: float | None = None, pickup_at: datetime | None = None) -> dict:
    """거리(도로) · 차종 · 중량 · 조건 · 날씨 · 권역 수급 · 통행료 · 오지 공차 · 장재물 · 예약으로 적정 운임을 계산한다.
    market: 상차 권역 수급 지수 (store.market_status). 없으면 가상 수요 · 공급만으로 계산.
    pickup_at: 상차 예약 시각 — 1시간 넘게 뒤면 그 시간대의 예상 날씨 · 수급(포아송)으로 서지를 계산하고,
               24시간 이상 미리 예약하면 조기 예약 할인 5%."""
    from src.engines.compliance_engine import tonnage
    from src.engines.packer.trucks import TRUCKS
    min_fare, per_km = TRUCK_TYPES.get(truck_type, TRUCK_TYPES["11톤 탑차"])
    distance = geo.road_km(origin, destination)
    base = _round5k(max(min_fare, per_km * distance))
    weight_fee, weight_detail = weight_surcharge(base, truck_type, weight_kg)

    rv = market_engine.reservation(pickup_at)
    region = market_engine.region_of(origin)
    if rv["use_forecast"]:  # 예약 시간대의 예상 수급 · 날씨 (지금 대기 중인 실제 화물 · 기사는 그때와 무관)
        mk = market_engine.market(region, at=datetime.fromisoformat(rv["pickup_at"]))
    else:
        mk = market or market_engine.market(region)
    w = mk["weather"]
    surge = mk["surge"]
    toll = market_engine.mock_toll_lookup(tonnage(truck_type), distance)
    dest = geo.geocode(destination)
    hub_km = min(geo.haversine_km(dest, h) for h in geo.HUBS.values())
    remote = market_engine.remote_surcharge(market_engine.region_of(destination), hub_km)
    truck = TRUCKS.get(truck_type)
    long_ = market_engine.long_cargo_surcharge(max_item_mm, truck.length if truck else None)
    factors = [
        {"key": "weight", "label": "적재 중량", "detail": weight_detail, "amount": weight_fee},
        {"key": "weather", "label": "기상 상태 (모의 기상)",
         "detail": f"{w['region']} {w['label']}" + (f" · +{w['multiplier']:.0%}" if w["multiplier"] else ""),
         "amount": _round5k(base * w["multiplier"])},
        {"key": "surge", "label": "수급 서지 (D/S)",
         "detail": (f"{mk['region']} 수요 {mk['demand']} ÷ 공급 {mk['supply']} = {mk['ds_ratio']}"
                    + (f" > 1.5 → +{surge:.0%}" if surge else " (1.5 이하 · 할증 없음)")
                    + (" · 러시아워" if mk["rush_hour"] else "")),
         "amount": _round5k(base * surge)},
        {"key": "urgent", "label": "긴급성", "detail": "당일 특송" if urgent else "일반", "amount": 20_000 if urgent else 0},
        {"key": "wait", "label": "현장 대기시간", "detail": f"예상 {wait_minutes}분",
         "amount": (wait_minutes // 30) * 10_000},
        {"key": "work", "label": "작업·적재 조건", "detail": "콜드체인 온도 관리" if cold_chain else "일반 적재",
         "amount": 30_000 if cold_chain else 0},
        {"key": "toll", "label": "고속도로 통행료 (룩업)", "detail": toll["note"], "amount": toll["amount"]},
        {"key": "remote", "label": "오지 공차 할증", "detail": remote["reason"]
         + (f" · +{remote['rate']:.0%}" if remote["rate"] else ""), "amount": _round5k(base * remote["rate"])},
        {"key": "long", "label": "장재물 할증", "detail": long_["reason"]
         + (f" · +{long_['rate']:.0%}" if long_["rate"] else ""), "amount": _round5k(base * long_["rate"])},
        {"key": "reservation", "label": "예약 배차",
         "detail": (f"{rv['pickup_at'].replace('T', ' ')} 상차 · {rv['hours_ahead']:.0f}시간 전 예약"
                    + (" · 조기 예약 -5%" if rv["discount"] else "")
                    + (" · 러시아워 시간대" if rv.get("rush_hour") else "")) if rv["reserved"] else "즉시 배차",
         "amount": -_round5k(base * rv["discount"])},
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
        "market": {k: mk[k] for k in ("region", "demand", "supply", "ds_ratio", "surge", "surge_on", "rush_hour")}
        | {"weather": w["label"], "weather_multiplier": w["multiplier"], "forecast": rv["use_forecast"]},
        "toll": toll, "reservation": rv,
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


PLATFORM_FEE_RATE = 0.03   # 플랫폼 수수료 (모의) — 기사 송금액 = 결제 금액 − 수수료


class BillingMixin:
    """차주 → 화주 청구 · 화주 결제 승인(모의 PG) · 결제 취소/환불 · 월별 인보이스 (Store 에 섞어 쓰는 Mixin)."""

    def market_status(self, region: str, exclude_id: str | None = None) -> dict:
        """권역 수급 지수: 실제 매칭 대기 화물(수요) · 빈 기사(공급) + 포아송 가상 콜 · 기본 공차.
        exclude_id: 수요에서 뺄 화물 (등록 중인 화물 자신 — 화주가 본 예상 운임과 같은 기준으로 계산)."""
        demand = sum(1 for o in self.orders.values()
                     if o["status"] == "MATCHING" and o["id"] != exclude_id
                     and market_engine.region_of(o["origin"]) == region)
        supply = sum(1 for d in self.drivers.values()
                     if market_engine.region_of(d["location"]) == region and self.driver_dispatchable(d)
                     and not self._active_run_of(d["id"]))
        return market_engine.market(region, demand, supply)

    def market_board(self) -> list[dict]:
        """전 권역 수급 현황 (관제 화면 · AI 관제)."""
        return sorted((self.market_status(r) for r in market_engine.BASE_DEMAND), key=lambda m: -m["ds_ratio"])

    def _fare_est(self, o: dict) -> dict:
        """AI 적정 운임 (도로거리 · 차종 · 적재 중량 · 조건 · 날씨 · 수급). 등록 시점 기준으로 한 번 계산해 저장한다."""
        est = o.get("fare_est")
        if est is None:
            w = o["packing"]["metrics"]["loaded_weight"] if o["packing"] else None
            items = (o["packing"] or {}).get("items") or []
            e = estimate(o["truck_type"], o["origin"], o["destination"], cold_chain=o["cold_chain"],
                         urgent=o["urgent"], weight_kg=w,
                         market=self.market_status(market_engine.region_of(o["origin"]), exclude_id=o["id"]),
                         max_item_mm=max((i["length"] for i in items), default=None),
                         pickup_at=o.get("pickup_at"))
            est = o["fare_est"] = {k: e[k] for k in ("recommended", "band", "base", "distance_km", "per_km",
                                                     "weight_kg", "market", "toll")} | {
                "factors": [f for f in e["factors"] if f["amount"]]}
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
        if o.get("payout_id"):
            raise ApiError(409, f"이미 기사에게 송금된 건입니다 ({o['payout_id']}). 송금 회수 후 취소해야 합니다.")
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
        photo = (o.get("pod") or {}).get("photo") or {}
        if not (photo.get("sha256") or photo.get("legacy")):
            raise ApiError(409, "인수증 사진 인증이 없는 건은 청구할 수 없습니다.")
        again = o["billing"] == "REFUNDED"
        o.update(extra_fee=extra_fee, toll_fee=toll_fee, memo=memo.strip(), requested_at=datetime.now())
        total = self._total(o)
        self._fsm(o, "request", self._actor("driver", driver_id), amount=total,
                  note=("재청구 · " if again else "")
                  + f"기본 {o['price'] + o['incentive']:,} + 추가 {extra_fee:,} + 통행료 {toll_fee:,}")
        return self.order_view(o)

    # ------------------------------------------------------------------ 기사 수익금 일괄 송금 (모의)
    def _payable_orders(self, driver_id: str | None = None) -> list[dict]:
        """화주 결제가 끝났고(PAID) 아직 기사에게 송금하지 않은 화물."""
        return [o for o in self.orders.values()
                if o["billing"] == "PAID" and (o.get("payment") or {}).get("status") == "APPROVED"
                and not o.get("payout_id") and (driver_id is None or o["driver_id"] == driver_id)]

    def payable_summary(self) -> list[dict]:
        """송금 대기: 기사별 건수 · 결제 합계 · 수수료 · 송금액."""
        rows: dict[str, dict] = {}
        for o in self._payable_orders():
            d = self.drivers.get(o["driver_id"], {})
            r = rows.setdefault(o["driver_id"], {"driver_id": o["driver_id"], "driver": d.get("name", "-"),
                                                 "plate": d.get("plate", "-"), "orders": [], "gross": 0})
            r["orders"].append(o["id"])
            r["gross"] += self._total(o)
        for r in rows.values():
            r["fee"] = round(r["gross"] * PLATFORM_FEE_RATE)
            r["net"] = r["gross"] - r["fee"]
            r["count"] = len(r["orders"])
        return sorted(rows.values(), key=lambda r: -r["net"])

    def run_payouts(self, by: str, driver_ids: list[str] | None = None) -> dict:
        """일괄 송금 실행: 기사별로 묶어 모의 이체(거래번호 발급) → 화물에 송금 번호 기록 → 감사 로그."""
        from src.engines.fsm.order_fsm import audit_entry
        now = datetime.now()
        done = []
        for r in self.payable_summary():
            if driver_ids and r["driver_id"] not in driver_ids:
                continue
            pid = self._next_id("PO", now)
            digest = hashlib.sha256(f"{pid}|{r['driver_id']}|{r['net']}|{now.isoformat()}".encode()).hexdigest()
            payout = {"id": pid, "driver_id": r["driver_id"], "driver": r["driver"], "orders": r["orders"],
                      "count": r["count"], "gross": r["gross"], "fee": r["fee"], "net": r["net"],
                      "fee_rate": PLATFORM_FEE_RATE, "bank": "모의 은행 ****-****-" + r["driver_id"][-3:],
                      "tid": f"MOCK-TR-{now:%Y%m%d%H%M%S}-{digest[:6].upper()}", "status": "TRANSFERRED",
                      "at": now, "by": by, "test": True}
            self.payouts.append(payout)
            for oid in r["orders"]:
                self.orders[oid]["payout_id"] = pid
            audit_entry(self, None, "billing", "payout", "PAID", "TRANSFERRED", by, amount=r["net"], at=now,
                        note=f"{r['driver_id']} {r['driver']} · {r['count']}건 · 결제 {r['gross']:,} − 수수료 {r['fee']:,} "
                             f"· {payout['tid']}", order_id=pid)
            done.append(payout)
        if not done:
            raise ApiError(409, "송금할 금액이 없습니다 (화주 결제가 끝난 미송금 건 없음).")
        return {"count": len(done), "total": sum(p["net"] for p in done), "payouts": [self._payout_view(p) for p in done]}

    @staticmethod
    def _payout_view(p: dict) -> dict:
        return {**p, "at": p["at"].isoformat(timespec="seconds") if isinstance(p["at"], datetime) else p["at"]}

    def payout_history(self, driver_id: str | None = None) -> list[dict]:
        rows = [p for p in self.payouts if driver_id is None or p["driver_id"] == driver_id]
        return [self._payout_view(p) for p in sorted(rows, key=lambda p: p["at"], reverse=True)]

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
