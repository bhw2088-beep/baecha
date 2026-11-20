"""
주문 상태 FSM · 감사 로그 점검 (서버 없이, 시드 데이터로).   실행: backend 폴더에서
    python -m tests.fsm_test.est_fsm

- 허용된 전이만 통과 (배차 → 운행 → 하차 → 청구 → 결제 → 취소 → 재청구)
- 순서를 건너뛰는 전이 · 이중 결제는 막힘
- 모든 전이가 감사 로그에 남고, 청구 · 결제 · 환불은 '재무 변경' + 금액으로 기록
"""

from tests._check import check, finish, section

from src.orm.database import Store
from src.utils.errors import ApiError
from src.engines.fsm import order_fsm

store = Store()  # 시드 데이터로 새로 만든 저장소 (DB 미사용)

section("전이표")
check("MATCHING → assign → ASSIGNED", order_fsm.next_state("order", "MATCHING", "assign") == "ASSIGNED")
check("DELIVERED → settle → SETTLED", order_fsm.next_state("order", "DELIVERED", "settle") == "SETTLED")
check("REQUESTED → pay → PAID", order_fsm.next_state("billing", "REQUESTED", "pay") == "PAID")
for kind, state, ev in (("order", "MATCHING", "start"), ("order", "ASSIGNED", "deliver"),
                        ("billing", "UNBILLED", "pay"), ("billing", "PAID", "pay")):
    try:
        order_fsm.next_state(kind, state, ev)
        check(f"{state} 에서 {ev} 금지", False)
    except order_fsm.TransitionError:
        check(f"{state} 에서 {ev} 금지", True)

section("감사 로그 (시드)")
check("시드 화물 이력 복원", len(store.audit) > 30, f"{len(store.audit)}건")
check("시드 결제 건은 재무 변경으로 기록", any(e["event"] == "pay" and e["financial"] and e["amount"] for e in store.audit))

section("한 바퀴: 배차 → 운행 → 하차 → 청구 → 결제")
o = next(o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"])
driver = o["offer"]["driver_id"]
before = len(store.audit)
try:
    store.start_run(driver, o["id"])
    check("배차 전 운행 시작 거부", False)
except ApiError:
    check("배차 전 운행 시작 거부", True)
store.accept_offer(driver, o["id"])
check("수락 → ASSIGNED", o["status"] == "ASSIGNED")
store.start_run(driver, o["id"])
check("상차 → IN_TRANSIT", o["status"] == "IN_TRANSIT")
try:
    store.complete_run(driver, o["id"], "홍길동")
    check("인수증 사진 없으면 하차 완료 불가", False)
except ApiError as e:
    check("인수증 사진 없으면 하차 완료 불가", e.status == 400 and "사진" in e.detail)
store.complete_run(driver, o["id"], "홍길동", photo={"filename": "pod.jpg", "size": 1024, "sha256": "ab" * 32})
check("인수증에 사진 SHA-256 기록", o["pod"]["photo"]["sha256"] == "ab" * 32)
check("하차 → DELIVERED + 청구 UNBILLED", o["status"] == "DELIVERED" and o["billing"] == "UNBILLED")
try:
    store.approve_invoice(o["shipper_id"], o["id"])
    check("청구 전 결제 승인 거부", False)
except ApiError:
    check("청구 전 결제 승인 거부", True)
store.request_invoice(driver, o["id"], extra_fee=10_000, toll_fee=5_000, memo="")
check("청구 → REQUESTED", o["billing"] == "REQUESTED")
store.approve_invoice(o["shipper_id"], o["id"])
check("결제 승인 → PAID + SETTLED", o["billing"] == "PAID" and o["status"] == "SETTLED")
try:
    store.approve_invoice(o["shipper_id"], o["id"])
    check("이중 결제 거부", False)
except ApiError:
    check("이중 결제 거부", True)

trail = store.audit_log(o["id"])[::-1]
events = [e["event"] for e in trail]
check("이력 순서", events[-7:] == ["assign", "start", "deliver", "deliver", "request", "pay", "settle"], " → ".join(events))
fin = [e for e in trail if e["financial"]]
total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
check("재무 변경은 금액과 함께", fin and all(e["amount"] == total for e in fin), f"{len(fin)}건 · {total:,}원")
check("누가 했는지 기록", all(e["actor"] for e in trail[-7:]), trail[-1]["actor"])
check("새 이력만 추가 (기존 이력 불변)", len(store.audit) == before + 7, f"+{len(store.audit) - before}건")

section("모의 PG: 결제 승인 · 취소(환불) · 재청구")
p = o["payment"]
check("모의 PG 거래번호 · 승인번호", p["tid"].startswith("MOCK-") and len(p["approval_no"]) == 8 and p["test"],
      f"{p['tid']} · {p['method_label']}")
check("공급가액 + 부가세 = 결제 금액", p["supply"] + p["vat"] == p["amount"] == total)
store.refund_payment(o["id"], "테스트 취소")
check("취소 → 청구 REFUNDED · 화물 DELIVERED", o["billing"] == "REFUNDED" and o["status"] == "DELIVERED")
check("결제 기록은 취소 상태로 남음", o["payment"]["status"] == "CANCELLED" and o["payments"][0]["status"] == "CANCELLED")
try:
    store.refund_payment(o["id"], "중복")
    check("이중 취소 거부", False)
except ApiError:
    check("이중 취소 거부", True)
store.request_invoice(driver, o["id"], extra_fee=0, toll_fee=5_000, memo="재청구")
check("재청구 → REQUESTED", o["billing"] == "REQUESTED")
store.approve_invoice(o["shipper_id"], o["id"], "transfer")
check("재결제 → PAID + SETTLED (새 거래번호)", o["status"] == "SETTLED" and o["payment"]["tid"] != p["tid"]
      and len(o["payments"]) == 2)
refund_events = [e for e in store.audit_log(o["id"]) if e["event"] == "refund"]
check("취소도 재무 감사 로그에", refund_events and refund_events[0]["financial"], refund_events[0]["note"])
inv = store.monthly_invoice(o["shipper_id"])
check("월별 인보이스 (취소분 제외)", inv["total"] == sum(ln["amount"] for ln in inv["lines"])
      and all(ln["tid"] != p["tid"] for ln in inv["lines"]), f"{inv['period']} · {inv['count']}건 · {inv['total']:,}원")

section("기사 수익금 일괄 송금 (모의)")
pend = store.payable_summary()
check("송금 대기 = 결제 완료 · 미송금 건", pend and all(r["net"] == r["gross"] - r["fee"] for r in pend),
      f"{len(pend)}명 · {sum(r['net'] for r in pend):,}원")
res = store.run_payouts("테스트")
check("일괄 송금 → 기사별 거래번호", res["count"] == len(pend) and all(p["tid"].startswith("MOCK-TR-") for p in res["payouts"]))
check("송금 후 대기 없음", store.payable_summary() == [])
check("송금 감사 로그 (재무)", any(e["event"] == "payout" and e["financial"] for e in store.audit))
paid = next(x for x in store.orders.values() if x.get("payout_id"))
try:
    store.refund_payment(paid["id"], "테스트")
    check("송금된 건 결제 취소 불가", False)
except ApiError as e:
    check("송금된 건 결제 취소 불가", e.status == 409)
try:
    store.run_payouts("테스트")
    check("송금할 금액 없으면 409", False)
except ApiError as e:
    check("송금할 금액 없으면 409", e.status == 409)
check("해시 체인 정상", order_fsm.verify_chain(store.audit)["ok"])

finish()
