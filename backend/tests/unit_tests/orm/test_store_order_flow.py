"""
메모리 저장소 (orm/database.Store) — 화물 한 바퀴 · 모의 PG · 기사 송금 단위 테스트.

배차 → 운행 → 하차(인수증) → 청구 → 결제 → 취소 → 재청구, 그리고 모든 전이가 감사 로그에 남는지 본다.
테스트마다 시드 데이터로 새 저장소를 만든다 (store fixture).
"""

import pytest

from src.engines.fsm import order_fsm
from src.utils.errors import ApiError

PHOTO = {"filename": "pod.jpg", "size": 1024, "sha256": "ab" * 32}


def _offered(store):
    o = next(o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"])
    return o, o["offer"]["driver_id"]


def _delivered(store):
    o, driver = _offered(store)
    store.accept_offer(driver, o["id"])
    store.start_run(driver, o["id"])
    store.complete_run(driver, o["id"], "홍길동", photo=PHOTO)
    return o, driver


def _paid(store, method=None):
    o, driver = _delivered(store)
    store.request_invoice(driver, o["id"], extra_fee=10_000, toll_fee=5_000, memo="")
    store.approve_invoice(o["shipper_id"], o["id"], *([method] if method else []))
    return o, driver


# ---------------------------------------------------------------- 생성자 (시드)
def test_ctor_store_seed_data(seed_store):
    s = seed_store
    assert s.orders and s.drivers and s.shippers
    assert {o["status"] for o in s.orders.values()} >= {"MATCHING", "IN_TRANSIT", "DELIVERED"}
    assert any(o["offer"] for o in s.orders.values() if o["status"] == "MATCHING")


def test_ctor_store_seed_audit_is_valid_chain(seed_store):
    assert len(seed_store.audit) > 30
    assert order_fsm.verify_chain(seed_store.audit)["ok"]
    assert any(e["event"] == "pay" and e["financial"] and e["amount"] for e in seed_store.audit)


# ---------------------------------------------------------------- 배차 · 운행 · 하차
def test_accept_offer_smoke(store):
    o, driver = _offered(store)
    store.accept_offer(driver, o["id"])
    assert o["status"] == "ASSIGNED" and o["driver_id"] == driver


def test_start_run_actual_requires_assignment(store):
    o, driver = _offered(store)
    with pytest.raises(ApiError):
        store.start_run(driver, o["id"])
    store.accept_offer(driver, o["id"])
    store.start_run(driver, o["id"])
    assert o["status"] == "IN_TRANSIT"


def test_complete_run_actual_requires_photo(store):
    o, driver = _offered(store)
    store.accept_offer(driver, o["id"])
    store.start_run(driver, o["id"])
    with pytest.raises(ApiError) as ex:
        store.complete_run(driver, o["id"], "홍길동")
    assert ex.value.status == 400 and "사진" in ex.value.detail
    assert o["status"] == "IN_TRANSIT"


def test_complete_run_actual_delivers_and_records_photo_hash(store):
    o, _ = _delivered(store)
    assert o["status"] == "DELIVERED" and o["billing"] == "UNBILLED"
    assert o["pod"]["photo"]["sha256"] == PHOTO["sha256"]


# ---------------------------------------------------------------- 청구 · 결제
def test_approve_invoice_actual_rejected_before_request(store):
    o, _ = _delivered(store)
    with pytest.raises(ApiError):
        store.approve_invoice(o["shipper_id"], o["id"])


def test_approve_invoice_smoke(store):
    o, _ = _paid(store)
    assert o["billing"] == "PAID" and o["status"] == "SETTLED"


def test_approve_invoice_actual_double_payment_rejected(store):
    o, _ = _paid(store)
    with pytest.raises(ApiError):
        store.approve_invoice(o["shipper_id"], o["id"])


def test_audit_actual_full_cycle_trail(store):
    before = len(store.audit)
    o, _ = _paid(store)
    trail = store.audit_log(o["id"])[::-1]
    assert [e["event"] for e in trail][-7:] == ["assign", "start", "deliver", "deliver", "request", "pay", "settle"]
    assert len(store.audit) == before + 7, "기존 이력은 그대로 두고 새 이력만 추가"
    assert all(e["actor"] for e in trail[-7:])
    total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
    fin = [e for e in trail if e["financial"]]
    assert fin and all(e["amount"] == total for e in fin)
    assert order_fsm.verify_chain(store.audit)["ok"]


# ---------------------------------------------------------------- 모의 PG
def test_mock_pg_actual_payment_record(store):
    o, _ = _paid(store)
    p = o["payment"]
    total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
    assert p["tid"].startswith("MOCK-") and len(p["approval_no"]) == 8 and p["test"] is True
    assert p["supply"] + p["vat"] == p["amount"] == total


def test_refund_payment_actual_reverts_and_blocks_double_refund(store):
    o, _ = _paid(store)
    store.refund_payment(o["id"], "테스트 취소")
    assert o["billing"] == "REFUNDED" and o["status"] == "DELIVERED"
    assert o["payment"]["status"] == "CANCELLED" and o["payments"][0]["status"] == "CANCELLED"
    refund = [e for e in store.audit_log(o["id"]) if e["event"] == "refund"]
    assert refund and refund[0]["financial"]
    with pytest.raises(ApiError):
        store.refund_payment(o["id"], "중복")


def test_rebill_actual_new_transaction_after_refund(store):
    o, driver = _paid(store)
    first_tid = o["payment"]["tid"]
    store.refund_payment(o["id"], "취소")
    store.request_invoice(driver, o["id"], extra_fee=0, toll_fee=5_000, memo="재청구")
    assert o["billing"] == "REQUESTED"
    store.approve_invoice(o["shipper_id"], o["id"], "transfer")
    assert o["status"] == "SETTLED" and o["payment"]["tid"] != first_tid and len(o["payments"]) == 2


def test_monthly_invoice_actual_excludes_cancelled(store):
    o, driver = _paid(store)
    cancelled = o["payment"]["tid"]
    store.refund_payment(o["id"], "취소")
    inv = store.monthly_invoice(o["shipper_id"])
    assert inv["total"] == sum(ln["amount"] for ln in inv["lines"])
    assert all(ln["tid"] != cancelled for ln in inv["lines"])


# ---------------------------------------------------------------- 기사 송금 (모의)
def test_payable_summary_smoke(store):
    _paid(store)
    pend = store.payable_summary()
    assert pend and all(r["net"] == r["gross"] - r["fee"] for r in pend)


def test_run_payouts_actual(store):
    _paid(store)
    pend = store.payable_summary()
    res = store.run_payouts("테스트")
    assert res["count"] == len(pend) and all(p["tid"].startswith("MOCK-TR-") for p in res["payouts"])
    assert store.payable_summary() == []
    assert any(e["event"] == "payout" and e["financial"] for e in store.audit)
    with pytest.raises(ApiError) as ex:
        store.run_payouts("다시")
    assert ex.value.status == 409


def test_refund_payment_actual_blocked_after_payout(store):
    _paid(store)
    store.run_payouts("테스트")
    paid = next(x for x in store.orders.values() if x.get("payout_id"))
    with pytest.raises(ApiError) as ex:
        store.refund_payment(paid["id"], "테스트")
    assert ex.value.status == 409
    assert order_fsm.verify_chain(store.audit)["ok"]
