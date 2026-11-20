"""
메모리 저장소 (orm/database.Store) — 회원가입 컴플라이언스 · 중복 방지 · 매칭 하드 필터 단위 테스트.
"""

import pytest

from src.engines import compliance_engine as comp
from src.utils.errors import ApiError

DOCS = [{"kind": k, "filename": f"{k}.jpg", "size": 1000} for k in comp.REQUIRED_DRIVER_DOCS]
SHIPPER = dict(role="shipper", password="password1", name="담당", company="배차테스트물류", biz_no="105-87-12345",
               owner_name="홍길동", open_date="20200115")


def _driver(store, **kw):
    base = dict(role="driver", email="t@x.com", password="password1", name="테스트", phone="010",
                plate="경기 81바 1234", truck_type="5톤 윙바디", biz_no="305-88-12340", base_location="경기 평택시",
                documents=DOCS)
    return store.register(**{**base, **kw})


def _shipper(store, email, phone, **kw):
    return store.register(**{**SHIPPER, "email": email, "phone": phone, **kw})


# ---------------------------------------------------------------- 기사 가입
def test_register_driver_smoke(store):
    res = _driver(store, email="ok@x.com")
    d = store.drivers[res["user"]["org_id"]]
    assert d["approved"] is False and d["plate_type"] == "COMMERCIAL_YELLOW" and d["tonnage"] == 5.0


@pytest.mark.parametrize("kw, word", [
    (dict(email="a@x.com", biz_no="123-45-67890"), "Mod-10"),
    (dict(email="b@x.com", biz_no="220-81-62517"), "폐업"),
    (dict(email="c@x.com", plate="경기 12가 3456"), "자가용"),
    (dict(email="d@x.com", documents=DOCS[:2]), "필수 서류"),
])
def test_register_driver_actual_rejections(store, kw, word):
    with pytest.raises(ApiError) as ex:
        _driver(store, **kw)
    assert word in ex.value.detail


def test_decide_driver_actual_approval_enables_dispatch(store):
    did = _driver(store, email="ok@x.com")["user"]["org_id"]
    d = store.drivers[did]
    assert not store.driver_dispatchable(d)
    store.decide_driver(did, True, by="테스트")
    assert store.driver_dispatchable(d)
    assert any(e["event"] == "approve" and e["order_id"] == did for e in store.audit)


# ---------------------------------------------------------------- 중복 가입 방지
def test_register_actual_duplicates_rejected(store):
    _driver(store, email="ok@x.com")
    cases = [
        (dict(role="driver", email="ok@x.com", password="password1", name="중복", phone="010-9999-0001"), "이메일"),
    ]
    for kw, word in cases:
        with pytest.raises(ApiError) as ex:
            store.register(**kw)
        assert ex.value.status == 409 and word in ex.value.detail
    for kw, word in [
        (dict(email="dup1@x.com", phone="010", plate="경기 82바 5555", biz_no="124-81-00998"), "연락처"),
        (dict(email="dup2@x.com", phone="010-9999-0002", plate="경기81바1234",
              biz_no=comp.with_check_digit("105871234")), "차량 번호"),
        (dict(email="dup3@x.com", phone="010-9999-0003", plate="경기 83바 5555", biz_no="3058812340"),
         "사업자등록번호"),
    ]:
        with pytest.raises(ApiError) as ex:
            _driver(store, **kw)
        assert ex.value.status == 409 and word in ex.value.detail, word


def test_register_actual_shipper_cannot_reuse_driver_biz_no(store):
    _driver(store, email="ok@x.com")
    with pytest.raises(ApiError) as ex:
        store.register(role="shipper", email="dup4@x.com", password="password1", name="담당", phone="010-9999-0004",
                       company="중복물류", biz_no="305-88-12340")
    assert ex.value.status == 409 and "차주로 등록된" in ex.value.detail


def test_register_actual_same_company_staff_allowed(store):
    s2 = store.register(role="shipper", email="staff2@x.com", password="password1", name="삼성물류 2팀",
                        phone="010-9999-0005", company="삼성물류", biz_no="124-81-00998", owner_name="김삼성",
                        open_date="19960101")
    assert s2["user"]["role"] == "shipper"


# ---------------------------------------------------------------- 화주 사업자 진위확인
def test_register_shipper_actual_suspended_business_rejected(store):
    with pytest.raises(ApiError) as ex:
        store.register(role="shipper", email="s@x.com", password="password1", name="담당", phone="",
                       company="테스트물류", biz_no="101-81-45901", owner_name="홍길동", open_date="20200115")
    assert "휴업" in ex.value.detail


@pytest.mark.parametrize("kw, word", [
    (dict(owner_name=""), "모두 입력"), (dict(open_date=" "), "모두 입력"),
    (dict(company="다른물류"), "기업명"), (dict(owner_name="김철수"), "대표자명"),
    (dict(open_date="20200116"), "개업일자"), (dict(biz_no=comp.with_check_digit("777771234")), "등록부에 없는"),
])
def test_register_shipper_actual_identity_mismatch(store, kw, word):
    with pytest.raises(ApiError) as ex:
        _shipper(store, "id@x.com", "010-8888-0001", **kw)
    assert ex.value.status == 400 and word in ex.value.detail


def test_register_shipper_actual_normalizes_company_and_date(store):
    ok = _shipper(store, "id7@x.com", "010-8888-0007", company="(주) 배차테스트물류", open_date="2020-01-15")
    assert store.shippers[ok["user"]["org_id"]]["owner_name"] == "홍길동"


# ---------------------------------------------------------------- 매칭 하드 필터
def test_driver_dispatchable_actual_private_plate_and_blacklist_excluded(store):
    assert not store.driver_dispatchable(store.drivers["D-008"])          # 자가용 번호판
    assert store.drivers["D-004"].get("blacklisted") is True              # 평점 미달 자동 제한
    o = next(o for o in store.orders.values() if o["status"] == "MATCHING")
    ids = {r["id"] for r in store._evaluate(o)}
    assert "D-008" not in ids and "D-004" not in ids


def test_set_blacklist_actual_release_restores_dispatch(store):
    store.set_blacklist("D-004", False, "테스트 해제", by="테스트")
    assert store.driver_dispatchable(store.drivers["D-004"])
