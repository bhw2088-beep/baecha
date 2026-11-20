"""매칭 엔진 (engines/matcher: cf_score · ml_ranker · hungarian) 단위 테스트."""

import pytest

from src.engines.matcher import hungarian, ml_ranker
from src.engines.matcher.cf_score import cf_score

FEAT = dict(pickup_km=5, util=0.8, fare_gap=0.05, trip_km=200, cold=0, urgent=0, accept_rate=0.8, cf_score=0.8,
            rating=4.8, rush_hour=0)
FAR = dict(FEAT, pickup_km=55, cf_score=0.2, rating=2.5, rush_hour=1, fare_gap=-0.2)


# ---------------------------------------------------------------- cf_score (권역 선호도)
def test_cf_score_smoke():
    s = cf_score("경기 평택시", "경기 화성시", "부산항", [], [])
    assert 0 <= s <= 1


def test_cf_score_actual_home_region_scores_higher():
    assert cf_score("경기 평택시", "경기 화성시", "부산항", [], []) > cf_score("부산항", "경기 화성시", "서울 중구", [], [])


def test_cf_score_actual_exact_weights():
    assert cf_score("부산항", "경기 화성시", "서울 중구", [], []) == 0.5
    assert cf_score("경기 평택시", "경기 화성시", "부산항", [], []) == 0.7          # +0.20 상차 권역
    assert cf_score("경기 평택시", "부산항", "경기 화성시", [], []) == 0.65         # +0.15 하차 권역
    assert cf_score("부산항", "경기 화성시", "서울 중구", ["경기"] * 5, []) == 0.74  # +0.08 × 최대 3회


def test_cf_score_actual_declines_lower_and_clamped():
    assert cf_score("경기 평택시", "경기 평택시", "부산항", [], ["경기"] * 3) < \
        cf_score("경기 평택시", "경기 평택시", "부산항", [], [])
    assert cf_score("부산항", "경기 화성시", "서울 중구", [], ["경기"] * 10) >= 0.0
    assert cf_score("경기 평택시", "경기 화성시", "경기 수원시", ["경기"] * 10, []) <= 1.0


# ---------------------------------------------------------------- ml_ranker (수락 예측)
def test_ctor_ml_ranker_features():
    assert len(ml_ranker.FEATURES) == 12
    assert ml_ranker.info().get("features") == ml_ranker.FEATURES


def test_predict_smoke():
    out = ml_ranker.predict([FEAT, FAR])
    assert len(out) == 2 and all(0 <= x <= 1 for x in out)


def test_mock_lgb_score_smoke():
    assert all(0 <= ml_ranker.mock_lgb_score(x) <= 1 for x in (FEAT, FAR))


def test_predict_actual_good_candidate_passes_threshold():
    assert ml_ranker.predict([FEAT])[0] >= 0.75


def test_predict_actual_bad_candidate_below_threshold():
    assert ml_ranker.predict([FAR])[0] < 0.75


def test_predict_actual_bad_weather_and_hot_market_lower_acceptance():
    assert ml_ranker.predict([dict(FEAT, weather=0.30, ds_ratio=4.0)])[0] < ml_ranker.predict([FEAT])[0]


def test_generate_synthetic_actual_shape_and_labels():
    x, y = ml_ranker.generate_synthetic(n=200, seed=1)
    assert len(x) == len(y) == 200 and all(len(r) == len(ml_ranker.FEATURES) for r in x)
    assert set(y) <= {0, 1} and 0 < sum(y) < 200


# ---------------------------------------------------------------- hungarian (최적 배정)
def test_assign_smoke():
    out = hungarian.assign(["O1"], ["D1"], {("O1", "D1"): 0.9})
    assert out == [("O1", "D1", 0.9)]


def test_assign_actual_empty_inputs():
    assert hungarian.assign([], ["D1"], {}) == [] and hungarian.assign(["O1"], [], {("O1", "D1"): 1}) == []


def test_assign_actual_global_optimum_beats_greedy():
    score = {("O1", "D1"): 0.9, ("O1", "D2"): 0.8, ("O2", "D1"): 0.85, ("O2", "D2"): 0.1}
    best = hungarian.assign(["O1", "O2"], ["D1", "D2"], score)
    greedy = hungarian.greedy(["O1", "O2"], ["D1", "D2"], score)
    assert sorted((o, d) for o, d, _ in best) == [("O1", "D2"), ("O2", "D1")]
    assert sum(s for *_, s in best) > sum(s for *_, s in greedy)


def test_assign_actual_skips_infeasible_pairs():
    out = hungarian.assign(["O1", "O2"], ["D1", "D2"], {("O1", "D1"): 0.5})
    assert out == [("O1", "D1", 0.5)]


def test_assign_actual_one_driver_per_order():
    score = {(o, d): 0.5 for o in ("O1", "O2", "O3") for d in ("D1", "D2")}
    out = hungarian.assign(["O1", "O2", "O3"], ["D1", "D2"], score)
    assert len(out) == 2 and len({d for _, d, _ in out}) == 2


def test_nearest_actual_picks_closest_in_order():
    km = {("O1", "D1"): 5.0, ("O1", "D2"): 2.0, ("O2", "D1"): 1.0, ("O2", "D2"): 9.0}
    assert hungarian.nearest(["O1", "O2"], ["D1", "D2"], km) == [("O1", "D2", 2.0), ("O2", "D1", 1.0)]


@pytest.mark.parametrize("fn", [hungarian.greedy, hungarian.nearest])
def test_baselines_actual_never_reuse_driver(fn):
    data = {(o, d): 1.0 for o in ("O1", "O2", "O3") for d in ("D1", "D2")}
    out = fn(["O1", "O2", "O3"], ["D1", "D2"], data)
    assert len({d for _, d, _ in out}) == len(out) == 2
