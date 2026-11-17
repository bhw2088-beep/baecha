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

감사 로그 = SHA-256 Append-Only 해시 체인 (models/audit.py)
  각 줄의 current_hash = SHA256(직전 줄 current_hash | 이 줄 내용) → 한 줄이라도 고치면 verify_chain 에서 걸린다.
"""

from __future__ import annotations

import hashlib
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
    # 관제 · 추적 이벤트 (화물 상태는 바꾸지 않고 기록만)
    "deviation": "경로 이탈 감지", "delay": "도착 지연 감지", "reoutsource": "불법 재위탁 의심",
    "blacklist": "기사 이용 제한", "unblacklist": "기사 제한 해제", "resolve": "이상 감지 조치",
    "approve": "가입 승인", "reject": "가입 반려", "payout": "기사 수익금 송금",
}
FINANCIAL = {"request", "pay", "refund", "payout"}


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


GENESIS = "0" * 64


def entry_hash(e: dict, previous_hash: str) -> str:
    """감사 로그 한 줄의 SHA-256 (직전 해시 + 내용). 시각은 초 단위 ISO 문자열로 고정."""
    at = e["at"].isoformat(timespec="seconds") if isinstance(e["at"], datetime) else str(e["at"])[:19]
    body = "|".join(str(x) for x in (previous_hash, e["id"], at, e["order_id"], e["kind"], e["event"],
                                      e["from"], e["to"], e["actor"], e["amount"], e["note"]))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def audit_entry(store, o: dict | None, kind: str, event: str, before, after, actor: str,
                note: str = "", amount: int | None = None, at: datetime | None = None,
                order_id: str | None = None) -> dict:
    """감사 로그 한 줄을 남긴다 (store.audit 에 추가 → DB audit_logs 로 저장).
    직전 줄의 current_hash 를 previous_hash 로 이어 SHA-256 해시 체인을 만든다 (Append-Only)."""
    at = at or datetime.now()
    store._audit_seq = getattr(store, "_audit_seq", len(store.audit)) + 1
    prev = store.audit[-1].get("current_hash") or GENESIS if store.audit else GENESIS
    e = {"id": f"AUD-{store._audit_seq:06d}", "at": at, "order_id": o["id"] if o else (order_id or "-"),
         "kind": kind, "event": event,
         "label": EVENT_LABEL.get(event, event), "from": before, "to": after, "actor": actor,
         "financial": event in FINANCIAL, "amount": amount, "note": note}
    e["previous_hash"] = prev
    e["current_hash"] = entry_hash(e, prev)
    store.audit.append(e)
    return e


def rebuild_chain(entries: list[dict]) -> None:
    """해시 체인 도입 전 데이터를 순서대로 다시 엮는다 (마이그레이션 1회용)."""
    prev = GENESIS
    for e in entries:
        e["previous_hash"] = prev
        e["current_hash"] = prev = entry_hash(e, prev)


def verify_chain(entries: list[dict]) -> dict:
    """위변조 검사: 처음부터 해시를 다시 계산해 저장된 값과 비교. 처음 어긋난 줄을 알려 준다."""
    prev = GENESIS
    for i, e in enumerate(entries):
        if e.get("previous_hash") != prev:
            return {"ok": False, "checked": i, "total": len(entries), "broken_at": e["id"],
                    "reason": "직전 줄의 해시와 연결이 끊김 (줄 삭제 · 순서 변경 의심)"}
        h = entry_hash(e, prev)
        if h != e.get("current_hash"):
            return {"ok": False, "checked": i, "total": len(entries), "broken_at": e["id"],
                    "reason": "내용이 바뀜 (해시 불일치)"}
        prev = h
    return {"ok": True, "checked": len(entries), "total": len(entries), "broken_at": None,
            "head": prev, "reason": "모든 줄의 해시가 일치"}


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
