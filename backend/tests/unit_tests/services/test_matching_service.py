"""매칭 서비스 함수 (services/matching_service) 단위 테스트 — 규칙 점수 · 차종 호환."""

import pytest

from src.services.matching_service import is_compatible, match_score


def test_match_score_smoke():
    s = match_score(10, 0.5)
    assert isinstance(s, float) and 0 <= s <= 1


@pytest.mark.parametrize("km, util, kw, score", [
    (0, 1.0, {}, 1.0),                                  # 가깝고 꽉 채우면 만점
    (50, 0.0, {}, 0.0),                                 # 반경 끝 · 적재 0
    (25, 0.5, {"radius_km": 50, "w_dist": 1.0}, 0.5),   # 거리만 보면 반경의 절반 = 0.5
    (0, 0.0, {"w_dist": 0.6}, 0.6),                     # 거리 가중치 60%
    (100, 1.0, {"w_dist": 0.6}, 0.4),                   # 반경 밖은 거리 점수 0 (음수 아님)
    (0, 1.5, {"w_dist": 0.0}, 1.0),                     # 적재율은 1 로 자름
])
def test_match_score_actual(km, util, kw, score):
    assert match_score(km, util, **kw) == pytest.approx(score)


def test_is_compatible_smoke():
    assert isinstance(is_compatible("5톤 윙바디", "5톤 윙바디", False), bool)


@pytest.mark.parametrize("driver, cargo, cold, ok", [
    ("5톤 윙바디", "5톤 윙바디", False, True),
    ("11톤 탑차", "5톤 윙바디", False, True),       # 2.2배 → 허용
    ("25톤 무진동", "5톤 윙바디", False, False),    # 5배 → 너무 큼
    ("2.5톤 카고", "5톤 윙바디", False, False),     # 작음
    ("5톤 윙바디", "5톤 냉동탑차", True, False),    # 냉장 조건인데 일반 차량
    ("11톤 냉동탑차", "5톤 냉동탑차", True, True),
])
def test_is_compatible_actual(driver, cargo, cold, ok):
    assert is_compatible(driver, cargo, cold) is ok
