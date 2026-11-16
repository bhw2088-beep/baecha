"""
주문 상태 FSM(유한 상태 기계) + 감사 로그(Audit Trail)

화물 상태와 청구 상태는 반드시 이 모듈의 transition() 으로만 바꾼다.
  - 허용된 전이만 통과하고, 그 외(예: 배송 전 정산, 이중 승인)는 409 오류로 막는다.
  - 모든 전이는 '언제 · 누가 · 무엇을 · 어떤 상태에서 어떤 상태로 · 금액'을 감사 로그에 남긴다
    (특히 청구 · 결제 · 환불 같은 재무 변경).

화물 상태
  MATCHING ──assign──▶ ASSIGNED ──start──▶ IN_TRANSIT ──deliver──▶ DELIVERED ──settle──▶ SETTLED
                                                                       ▲                      │
                                                                       └──────unsettle────────┘ (결제 취소)
청구(정산) 상태
  (없음) ──deliver──▶ UNBILLED ──request──▶ REQUESTED ──pay──▶ PAID ──refund──▶ REFUNDED
                                                                 REFUNDED ──request──▶ REQUESTED (재청구)
"""

from __future__ import annotations

from datetime import datetime

ORDER_STATES = ("MATCHING", "ASSIGNED", "IN_TRANSIT", "DELIVERED", "SETTLED")
BILLING_STATES = ("UNBILLED", "REQUESTED", "PAID", "REFUNDED")

# (현재 상태, 이벤트) → 다음 상태
ORDER_TRANSITIONS: dict[tuple[str, str], str] = {
    ("MATCHING", "assign"): "ASSIGNED",
    ("ASSIGNED", "start"): "IN_TRANSIT",
    ("IN_TRANSIT", "deliver"): "DELIVERED",
    ("DELIVERED", "settle"): "SETTLED",
    ("SETTLED", "unsettle"): "DELIVERED",
}
BILLING_TRANSITIONS: dict[tuple[str | None, str], str] = {
    (None, "deliver"): "UNBILLED",
    ("UNBILLED", "request"): "REQUESTED",
    ("REQUESTED", "pay"): "PAID",
    ("PAID", "refund"): "REFUNDED",
    ("REFUNDED", "request"): "REQUESTED",
}
EVENT_LABEL = {
    "create": "화물 등록", "assign": "배차 확정", "start": "상차 완료 · 운행 시작", "deliver": "하차 완료 · 전자인수증",
    "request": "운송비 청구", "pay": "결제 승인 · 정산", "settle": "정산 완료", "refund": "결제 취소 · 환불",
    "unsettle": "정산 취소",
}
FINANCIAL = {"request", "pay", "refund"}


class TransitionError(Exception):
    """허용되지 않는 상태 전이."""

    def __init__(self, kind: str, state: str | None, event: str):
        self.kind, self.state, self.event = kind, state, event
        label = "화물" if kind == "order" else "청구"
        super().__init__(f"{label} 상태 '{state or '없음'}'에서 '{EVENT_LABEL.get(event, event)}'을(를) 할 수 없습니다.")


def can(kind: str, state: str | None, event: str) -> bool:
    table = ORDER_TRANSITIONS if kind == "order" else BILLING_TRANSITIONS
    return (state, event) in table


def next_state(kind: str, state: str | None, event: str) -> str:
    table = ORDER_TRANSITIONS if kind == "order" else BILLING_TRANSITIONS
    try:
        return table[(state, event)]
    except KeyError:
        raise TransitionError(kind, state, event) from None


def audit_entry(store, o: dict, kind: str, event: str, before, after, actor: str,
                note: str = "", amount: int | None = None, at: datetime | None = None) -> dict:
    """감사 로그 한 줄을 남긴다 (store.audit 에 추가 → DB audit_logs 로 저장)."""
    at = at or datetime.now()
    store._audit_seq = getattr(store, "_audit_seq", len(store.audit)) + 1
    e = {"id": f"AUD-{store._audit_seq:06d}", "at": at, "order_id": o["id"], "kind": kind, "event": event,
         "label": EVENT_LABEL.get(event, event), "from": before, "to": after, "actor": actor,
         "financial": event in FINANCIAL, "amount": amount, "note": note}
    store.audit.append(e)
    return e


def transition(store, o: dict, event: str, actor: str, *, note: str = "", amount: int | None = None,
               at: datetime | None = None) -> dict:
    """화물 상태 · 청구 상태를 이벤트 하나로 함께 전이시킨다.
    이벤트가 두 표 중 어디에도 맞지 않으면 TransitionError (아무것도 바꾸지 않음)."""
    order_ok = (o["status"], event) in ORDER_TRANSITIONS
    bill_ok = (o.get("billing"), event) in BILLING_TRANSITIONS
    if event == "deliver":  # 하차 완료는 화물(IN_TRANSIT→DELIVERED)과 청구(없음→UNBILLED)를 함께
        if not (order_ok and bill_ok):
            raise TransitionError("order", o["status"], event)
    elif event == "pay":     # 결제 승인 = 청구 PAID + 화물 SETTLED
        if not (bill_ok and (o["status"], "settle") in ORDER_TRANSITIONS):
            raise TransitionError("billing", o.get("billing"), event)
    elif event == "refund":  # 결제 취소 = 청구 REFUNDED + 화물 SETTLED→DELIVERED
        if not (bill_ok and (o["status"], "unsettle") in ORDER_TRANSITIONS):
            raise TransitionError("billing", o.get("billing"), event)
    elif not (order_ok or bill_ok):
        raise TransitionError("order" if event in ("assign", "start") else "billing",
                              o["status"] if event in ("assign", "start") else o.get("billing"), event)

    at = at or datetime.now()
    if event in ("assign", "start", "deliver"):
        before = o["status"]
        o["status"] = next_state("order", before, event)
        audit_entry(store, o, "order", event, before, o["status"], actor, note, amount, at)
    if event in ("deliver", "request", "pay", "refund"):
        before = o.get("billing")
        o["billing"] = next_state("billing", before, event)
        audit_entry(store, o, "billing", event, before, o["billing"], actor, note, amount, at)
    if event in ("pay", "refund"):
        sub = "settle" if event == "pay" else "unsettle"
        before = o["status"]
        o["status"] = next_state("order", before, sub)
        audit_entry(store, o, "order", sub, before, o["status"], actor, note, amount, at)
    return o
