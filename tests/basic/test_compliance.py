"""
가입 컴플라이언스 점검 (서버 없이).   실행: 수정본 폴더에서  python tests/basic/test_compliance.py

- 사업자번호 Modulus-10: 정상 번호 셋 통과 · 가짜/검증 숫자 오류 · 형식 오류 · 모의 휴업/폐업 실패
- 번호판 plate_type: 사업용(노란색 바 · 사 · 아 · 자 · 배) / 자가용 / 렌터카 / 형식 오류
- 톤수 파싱 · 필수 서류 3종
- 회원가입: 실패 번호 · 자가용 번호판 · 서류 누락 거절, 정상 가입은 승인 대기 → 승인 후 매칭 후보
- 매칭 하드 필터: 자가용 번호판(D-008) · 블랙리스트(D-004) 기사는 후보 제외
"""

from _common import check, finish, section

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
                   biz_no="101-81-45901", owner_name="홍길동", open_date="20200115")
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

section("회원가입 중복 방지")


def rejected(label: str, word: str, **kw):
    try:
        store.register(**kw)
        check(label, False, "가입됨")
    except ApiError as e:
        check(label, e.status == 409 and word in e.detail, e.detail)


rejected("같은 이메일 → 거절", "이메일", **{**dict(role="driver", email="ok@x.com", password="password1", name="중복",
                                         phone="010-9999-0001")})
try:
    reg(email="dup1@x.com", phone="010", plate="경기 82바 5555", biz_no="124-81-00998")
    check("같은 연락처(010) → 거절", False, "가입됨")
except ApiError as e:
    check("같은 연락처(010) → 거절", e.status == 409 and "연락처" in e.detail, e.detail)
try:
    reg(email="dup2@x.com", phone="010-9999-0002", plate="경기81바1234", biz_no=comp.with_check_digit("105871234"))
    check("같은 차량 번호(띄어쓰기 달라도) → 거절", False, "가입됨")
except ApiError as e:
    check("같은 차량 번호(띄어쓰기 달라도) → 거절", e.status == 409 and "차량 번호" in e.detail, e.detail)
try:
    reg(email="dup3@x.com", phone="010-9999-0003", plate="경기 83바 5555", biz_no="3058812340")
    check("다른 차주와 같은 사업자번호 → 거절", False, "가입됨")
except ApiError as e:
    check("다른 차주와 같은 사업자번호 → 거절", e.status == 409 and "사업자등록번호" in e.detail, e.detail)
rejected("차주 사업자번호로 화주 가입 → 거절", "차주로 등록된", role="shipper", email="dup4@x.com", password="password1",
         name="담당", phone="010-9999-0004", company="중복물류", biz_no="305-88-12340")
s2 = store.register(role="shipper", email="staff2@x.com", password="password1", name="삼성물류 2팀", phone="010-9999-0005",
                    company="삼성물류", biz_no="124-81-00998", owner_name="김삼성", open_date="19960101")
check("같은 회사(화주) 사업자번호로 담당자 추가 가입 → 허용", s2["user"]["role"] == "shipper")

section("화주 사업자 진위확인 (기업명 · 사업자번호 · 대표자명 · 개업일자 4개 일치)")
SHIP = dict(role="shipper", password="password1", name="담당", company="배차테스트물류", biz_no="105-87-12345",
            owner_name="홍길동", open_date="20200115")


def ship(email: str, phone: str, **kw):
    return store.register(**{**SHIP, "email": email, "phone": phone, **kw})


def ship_rejected(label: str, word: str, email: str, phone: str, **kw):
    try:
        ship(email, phone, **kw)
        check(label, False, "가입됨")
    except ApiError as e:
        check(label, e.status == 400 and word in e.detail, e.detail)


ship_rejected("대표자명 빈칸 → 거절", "모두 입력", "id1@x.com", "010-8888-0001", owner_name="")
ship_rejected("개업일자 빈칸 → 거절", "모두 입력", "id2@x.com", "010-8888-0002", open_date=" ")
ship_rejected("기업명 다름 → 거절", "기업명", "id3@x.com", "010-8888-0003", company="다른물류")
ship_rejected("대표자명 다름 → 거절", "대표자명", "id4@x.com", "010-8888-0004", owner_name="김철수")
ship_rejected("개업일자 다름 → 거절", "개업일자", "id5@x.com", "010-8888-0005", open_date="20200116")
ship_rejected("등록부에 없는 번호(국세청 키 없음) → 거절", "등록부에 없는", "id6@x.com", "010-8888-0006",
              biz_no=comp.with_check_digit("777771234"))
ok = ship("id7@x.com", "010-8888-0007", company="(주) 배차테스트물류", open_date="2020-01-15")
sid = ok["user"]["org_id"]
check("4개 일치 → 가입 ('(주)' · 띄어쓰기 · 날짜 구분자 무시)", store.shippers[sid]["owner_name"] == "홍길동",
      store.shippers[sid]["biz_check"]["note"])
r = comp.verify_business_identity(comp.with_check_digit("777771234"), "아무물류", "홍길동", "20200115",
                                  api_key="invalid-key", timeout=2)
check("국세청 호출 실패 → 통과시키지 않음 (확인 불가)", not r["ok"] and r["status"] == "UNAVAILABLE", r["note"])

section("매칭 하드 필터")
check("D-008 자가용 번호판 → 후보 제외", not store.driver_dispatchable(store.drivers["D-008"]))
check("D-004 평점 미달 → 자동 블랙리스트", store.drivers["D-004"].get("blacklisted") is True)
o = next(o for o in store.orders.values() if o["status"] == "MATCHING")
ids = {r["id"] for r in store._evaluate(o)}
check("평가 대상에서 D-008 · D-004 제외", "D-008" not in ids and "D-004" not in ids)
store.set_blacklist("D-004", False, "테스트 해제", by="테스트")
check("관제 해제 → 다시 후보", store.driver_dispatchable(store.drivers["D-004"]))

finish()
