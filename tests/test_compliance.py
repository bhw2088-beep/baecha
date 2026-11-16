"""
가입 컴플라이언스 점검 (서버 없이).   실행: 수정본 폴더에서  python tests/test_compliance.py

- 사업자번호 Modulus-10: 정상 번호 셋 통과 · 가짜/검증 숫자 오류 · 형식 오류 · 모의 휴업/폐업 실패
- 번호판 plate_type: 사업용(노란색 바 · 사 · 아 · 자 · 배) / 자가용 / 렌터카 / 형식 오류
- 톤수 파싱 · 필수 서류 3종
- 회원가입: 실패 번호 · 자가용 번호판 · 서류 누락 거절, 정상 가입은 승인 대기 → 승인 후 매칭 후보
- 매칭 하드 필터: 자가용 번호판(D-008) · 블랙리스트(D-004) 기사는 후보 제외
"""

from _check import check, finish, section

from backend.core.database import Store
from backend.core.errors import ApiError
from backend.engines import compliance_engine as comp

section("사업자번호 Modulus-10")
for b in comp.SAMPLE_VALID:
    r = comp.mock_business_status(b)
    check(f"정상 {comp.format_biz_no(b)} → 계속사업자", r["ok"] and r["status"] == "CONTINUING")
for b, code in comp.SAMPLE_INVALID.items():
    r = comp.mock_business_status(b)
    check(f"실패 {b} → {comp.STATUS_LABEL[code]}", not r["ok"] and r["status"] == code, r["label"])
check("하이픈 · 공백 무시", comp.mock_validate_business_number(" 124-81-00998 "))
check("검증 숫자 계산", comp.with_check_digit("124810099") == "1248100998")
check("키 없으면 모의 판정", comp.verify_business("1248100998")["source"] == "mock")

section("차량 번호판 plate_type")
for plate, t in [("경기 88바 7712", "COMMERCIAL_YELLOW"), ("서울 87아 5521", "COMMERCIAL_YELLOW"),
                 ("부산 72자 9081", "COMMERCIAL_YELLOW"), ("88사1102", "COMMERCIAL_YELLOW"), ("123배 4567", "COMMERCIAL_YELLOW"),
                 ("부산 11가 2233", "PRIVATE_WHITE"), ("12하 3456", "RENTAL"), ("ABC-123", "INVALID")]:
    check(f"{plate} → {t}", comp.classify_plate(plate)["plate_type"] == t)

section("톤수 · 서류")
check("톤수 파싱", [comp.tonnage(t) for t in ("1톤 카고", "2.5톤 카고", "11톤 냉동탑차", "25톤 무진동")] == [1, 2.5, 11, 25])
check("필수 서류 3종 누락 감지", len(comp.missing_driver_docs([{"kind": "license"}])) == 2)

section("회원가입 컴플라이언스")
store = Store()
docs = [{"kind": k, "filename": f"{k}.jpg", "size": 1000} for k in comp.REQUIRED_DRIVER_DOCS]


def reg(**kw):
    base = dict(role="driver", email="t@x.com", password="password1", name="테스트", phone="010",
                plate="경기 81바 1234", truck_type="5톤 윙바디", biz_no="305-88-12340", base_location="경기 평택시",
                documents=docs)
    base.update(kw)
    return store.register(**base)


for label, kw, msg in [
    ("검증 숫자 오류 사업자 거절", dict(email="a@x.com", biz_no="123-45-67890"), "Mod-10"),
    ("폐업 사업자 거절", dict(email="b@x.com", biz_no="220-81-62517"), "폐업"),
    ("자가용 번호판 거절", dict(email="c@x.com", plate="경기 12가 3456"), "자가용"),
    ("서류 누락 거절", dict(email="d@x.com", documents=docs[:2]), "필수 서류"),
]:
    try:
        reg(**kw)
        check(label, False, "가입됨")
    except ApiError as e:
        check(label, msg in e.detail, e.detail)
try:
    store.register(role="shipper", email="s@x.com", password="password1", name="담당", phone="", company="테스트물류",
                   biz_no="101-81-45901")
    check("휴업 화주 거절", False)
except ApiError as e:
    check("휴업 화주 거절", "휴업" in e.detail, e.detail)
res = reg(email="ok@x.com")
did = res["user"]["org_id"]
d = store.drivers[did]
check("정상 기사 가입 → 승인 대기 · 노란 번호판 · 톤수", d["approved"] is False and d["plate_type"] == "COMMERCIAL_YELLOW"
      and d["tonnage"] == 5.0)
check("승인 전 배차 불가", not store.driver_dispatchable(d))
store.decide_driver(did, True, by="테스트")
check("승인 후 배차 가능", store.driver_dispatchable(d))
check("승인 이력 감사 로그", any(e["event"] == "approve" and e["order_id"] == did for e in store.audit))

section("매칭 하드 필터")
check("D-008 자가용 번호판 → 후보 제외", not store.driver_dispatchable(store.drivers["D-008"]))
check("D-004 평점 미달 → 자동 블랙리스트", store.drivers["D-004"].get("blacklisted") is True)
o = next(o for o in store.orders.values() if o["status"] == "MATCHING")
ids = {r["id"] for r in store._evaluate(o)}
check("평가 대상에서 D-008 · D-004 제외", "D-008" not in ids and "D-004" not in ids)
store.set_blacklist("D-004", False, "테스트 해제", by="테스트")
check("관제 해제 → 다시 후보", store.driver_dispatchable(store.drivers["D-004"]))

finish()
