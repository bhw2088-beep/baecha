"""주문 상태 FSM · 감사 로그 해시 체인 (engines/fsm/order_fsm) 단위 테스트."""

from datetime import datetime

import pytest

from src.engines.fsm import order_fsm as fsm


class _FakeStore:
    """audit_entry 가 쓰는 최소 저장소 (audit 목록만)."""

    def __init__(self):
        self.audit = []


def _chain(n: int = 5) -> list[dict]:
    s = _FakeStore()
    for i in range(n):
        fsm.audit_entry(s, {"id": f"O-{i}"}, "order", "assign", "MATCHING", "ASSIGNED", "테스트",
                        amount=1000 * i, at=datetime(2026, 1, 1, 9, i))
    return s.audit


# ---------------------------------------------------------------- 생성자 · 표
def test_ctor_transition_error_message():
    e = fsm.TransitionError("order", "MATCHING", "start")
    assert (e.kind, e.state, e.event) == ("order", "MATCHING", "start")
    assert "MATCHING" in str(e)


def test_ctor_transition_tables_use_known_states():
    for (state, _), nxt in fsm.ORDER_TRANSITIONS.items():
        assert state in fsm.ORDER_STATES and nxt in fsm.ORDER_STATES
    for (state, _), nxt in fsm.BILLING_TRANSITIONS.items():
        assert (state is None or state in fsm.BILLING_STATES) and nxt in fsm.BILLING_STATES


def test_ctor_financial_events():
    assert fsm.FINANCIAL == {"request", "pay", "refund", "payout"}


# ---------------------------------------------------------------- next_state / can
def test_next_state_smoke():
    assert fsm.next_state("order", "MATCHING", "assign") == "ASSIGNED"


@pytest.mark.parametrize("kind, state, event, nxt", [
    ("order", "MATCHING", "assign", "ASSIGNED"),
    ("order", "ASSIGNED", "start", "IN_TRANSIT"),
    ("order", "IN_TRANSIT", "deliver", "DELIVERED"),
    ("order", "DELIVERED", "settle", "SETTLED"),
    ("order", "SETTLED", "unsettle", "DELIVERED"),
    ("billing", None, "deliver", "UNBILLED"),
    ("billing", "UNBILLED", "request", "REQUESTED"),
    ("billing", "REQUESTED", "pay", "PAID"),
    ("billing", "PAID", "refund", "REFUNDED"),
    ("billing", "REFUNDED", "request", "REQUESTED"),
])
def test_next_state_actual_allowed(kind, state, event, nxt):
    assert fsm.can(kind, state, event)
    assert fsm.next_state(kind, state, event) == nxt


@pytest.mark.parametrize("kind, state, event", [
    ("order", "MATCHING", "start"),      # 배차 전 운행
    ("order", "ASSIGNED", "deliver"),    # 운행 건너뛰기
    ("order", "SETTLED", "assign"),
    ("billing", "UNBILLED", "pay"),      # 청구 전 결제
    ("billing", "PAID", "pay"),          # 이중 결제
    ("billing", "REQUESTED", "refund"),
])
def test_next_state_actual_forbidden(kind, state, event):
    assert not fsm.can(kind, state, event)
    with pytest.raises(fsm.TransitionError):
        fsm.next_state(kind, state, event)


# ---------------------------------------------------------------- 감사 로그 해시 체인
def test_audit_entry_smoke():
    e = _chain(1)[0]
    assert e["previous_hash"] == fsm.GENESIS and len(e["current_hash"]) == 64
    assert e["id"] == "AUD-000001" and e["financial"] is False


def test_audit_entry_actual_links_previous_hash():
    chain = _chain(3)
    assert chain[1]["previous_hash"] == chain[0]["current_hash"]
    assert chain[2]["previous_hash"] == chain[1]["current_hash"]


def test_entry_hash_actual_deterministic_and_content_sensitive():
    e = _chain(1)[0]
    assert fsm.entry_hash(e, fsm.GENESIS) == e["current_hash"]
    assert fsm.entry_hash({**e, "amount": 1}, fsm.GENESIS) != e["current_hash"]
    assert fsm.entry_hash(e, "1" * 64) != e["current_hash"]


def test_verify_chain_smoke():
    v = fsm.verify_chain(_chain(5))
    assert v["ok"] is True and v["checked"] == 5 and v["broken_at"] is None


def test_verify_chain_actual_detects_modified_line():
    chain = [dict(e) for e in _chain(5)]
    chain[2]["amount"] += 1
    v = fsm.verify_chain(chain)
    assert v["ok"] is False and v["broken_at"] == chain[2]["id"]


def test_verify_chain_actual_detects_deleted_line():
    chain = _chain(5)
    del chain[1]
    assert fsm.verify_chain(chain)["ok"] is False


def test_verify_chain_actual_detects_reordered_lines():
    chain = _chain(4)
    chain[1], chain[2] = chain[2], chain[1]
    assert fsm.verify_chain(chain)["ok"] is False


def test_rebuild_chain_actual_repairs_legacy_entries():
    chain = [dict(e, previous_hash=None, current_hash=None) for e in _chain(3)]
    fsm.rebuild_chain(chain)
    assert fsm.verify_chain(chain)["ok"] is True
