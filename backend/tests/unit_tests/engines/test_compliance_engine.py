"""가입 컴플라이언스 엔진 (engines/compliance_engine) 단위 테스트 — 사업자 Mod-10 · 번호판 · 톤수 · 서류."""

import pytest

from src.engines import compliance_engine as comp


# ---------------------------------------------------------------- 상수 · 표본
def test_ctor_weights_and_required_docs():
    assert comp.WEIGHTS == (1, 3, 7, 1, 3, 7, 1, 3, 5)
    assert set(comp.REQUIRED_DRIVER_DOCS) == {"license", "cargo_license", "vehicle"}


def test_ctor_sample_numbers_are_consistent():
    assert all(comp.mock_validate_business_number(b) for b in comp.SAMPLE_VALID)
    assert set(comp.SAMPLE_INVALID.values()) <= set(comp.STATUS_LABEL)


# ---------------------------------------------------------------- 사업자번호
def test_normalize_and_format_biz_no_smoke():
    assert comp.normalize_biz_no(" 124-81-00998 ") == "1248100998"
    assert comp.format_biz_no("1248100998") == "124-81-00998"


def test_check_digit_actual():
    assert comp.check_digit("124810099") == 8
    assert comp.with_check_digit("124810099") == "1248100998"


@pytest.mark.parametrize("biz", comp.SAMPLE_VALID)
def test_mock_business_status_actual_valid_numbers_continuing(biz):
    r = comp.mock_business_status(biz)
    assert r["ok"] is True and r["status"] == "CONTINUING"


@pytest.mark.parametrize("biz, code", list(comp.SAMPLE_INVALID.items()))
def test_mock_business_status_actual_invalid_numbers(biz, code):
    r = comp.mock_business_status(biz)
    assert r["ok"] is False and r["status"] == code


def test_mock_validate_business_number_actual_one_digit_off_fails():
    good = "1248100998"
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert comp.mock_validate_business_number(good) and not comp.mock_validate_business_number(bad)
    assert not comp.mock_validate_business_number(None) and not comp.mock_validate_business_number("12345")


def test_verify_business_actual_without_key_uses_mock():
    assert comp.verify_business("1248100998")["source"] == "mock"


def test_verify_business_identity_actual_api_failure_is_not_ok():
    r = comp.verify_business_identity(comp.with_check_digit("777771234"), "아무물류", "홍길동", "20200115",
                                      api_key="invalid-key", timeout=2)
    assert r["ok"] is False and r["status"] == "UNAVAILABLE"


# ---------------------------------------------------------------- 번호판
def test_classify_plate_smoke():
    r = comp.classify_plate("경기 88바 7712")
    assert {"plate_type", "label", "usage_char", "dispatchable"} <= r.keys()


@pytest.mark.parametrize("plate, ptype", [
    ("경기 88바 7712", "COMMERCIAL_YELLOW"), ("서울 87아 5521", "COMMERCIAL_YELLOW"),
    ("부산 72자 9081", "COMMERCIAL_YELLOW"), ("88사1102", "COMMERCIAL_YELLOW"), ("123배 4567", "COMMERCIAL_YELLOW"),
    ("부산 11가 2233", "PRIVATE_WHITE"), ("12하 3456", "RENTAL"), ("ABC-123", "INVALID"), ("", "INVALID"),
])
def test_classify_plate_actual(plate, ptype):
    r = comp.classify_plate(plate)
    assert r["plate_type"] == ptype
    assert r["dispatchable"] is (ptype == "COMMERCIAL_YELLOW")


# ---------------------------------------------------------------- 톤수 · 서류 · 종합
@pytest.mark.parametrize("truck, tons", [("1톤 카고", 1.0), ("2.5톤 카고", 2.5), ("11톤 냉동탑차", 11.0),
                                         ("25톤 무진동", 25.0), ("트럭", 0.0)])
def test_tonnage_actual(truck, tons):
    assert comp.tonnage(truck) == tons


def test_missing_driver_docs_actual():
    assert len(comp.missing_driver_docs([{"kind": "license"}])) == 2
    assert comp.missing_driver_docs([{"kind": k} for k in comp.REQUIRED_DRIVER_DOCS]) == []
    assert len(comp.missing_driver_docs([])) == 3


def test_driver_compliance_smoke():
    docs = [{"kind": k} for k in comp.REQUIRED_DRIVER_DOCS]
    r = comp.driver_compliance("경기 88바 7712", "5톤 윙바디", docs, comp.mock_business_status("1248100998"))
    assert isinstance(r, dict)


def test_driver_compliance_actual_private_plate_and_missing_docs_are_issues():
    good_docs = [{"kind": k} for k in comp.REQUIRED_DRIVER_DOCS]
    ok = comp.driver_compliance("경기 88바 7712", "5톤 윙바디", good_docs, comp.mock_business_status("1248100998"))
    bad = comp.driver_compliance("부산 11가 2233", "5톤 윙바디", [], comp.mock_business_status("1234567890"))
    assert not ok.get("issues")
    assert len(bad["issues"]) >= 3
