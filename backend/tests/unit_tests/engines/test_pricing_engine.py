"""운임 · 수급 엔진 (engines/pricing_engine) 단위 테스트 — 모의 기상 · 포아송 수요 · 서지 · 통행료 · 할증 · 예약."""

import random
from datetime import datetime, timedelta

import pytest

from src.engines import pricing_engine as pe

AT = datetime(2026, 11, 17, 14)


# ---------------------------------------------------------------- 상수 · 표
def test_ctor_weather_table():
    assert set(pe.WEATHER) == {"CLEAR", "RAIN", "SNOW"}
    assert pe.WEATHER["CLEAR"]["multiplier"] == 0.0
    assert pe.WEATHER["RAIN"]["multiplier"] == 0.15
    assert pe.WEATHER["SNOW"]["multiplier"] == 0.30


def test_ctor_surge_constants():
    assert pe.SURGE_AT == 1.5 and pe.SURGE_MAX == 0.30


@pytest.mark.parametrize("month", range(1, 13))
def test_ctor_weather_distribution_sums_to_one(month):
    assert abs(sum(pe.weather_distribution(month).values()) - 1) < 1e-9


# ---------------------------------------------------------------- region_of / is_rush_hour
def test_region_of_smoke():
    assert isinstance(pe.region_of("경기 평택시"), str)


@pytest.mark.parametrize("addr, region", [("경기 평택시", "경기"), ("인천항 물류센터", "인천"), ("부산항", "부산"),
                                          ("", "기타")])
def test_region_of_actual(addr, region):
    assert pe.region_of(addr) == region


def test_is_rush_hour_actual():
    assert pe.is_rush_hour(datetime(2026, 1, 1, 8)) and pe.is_rush_hour(datetime(2026, 1, 1, 18))
    assert not pe.is_rush_hour(datetime(2026, 1, 1, 12)) and not pe.is_rush_hour(datetime(2026, 1, 1, 3))


# ---------------------------------------------------------------- 모의 기상
def test_mock_get_weather_condition_smoke():
    w = pe.mock_get_weather_condition("서울", AT)
    assert w["code"] in pe.WEATHER and w["multiplier"] == pe.WEATHER[w["code"]]["multiplier"]
    assert w["region"] == "서울"


def test_mock_get_weather_condition_actual_same_slot_same_weather():
    assert pe.mock_get_weather_condition("서울", AT)["code"] == pe.mock_get_weather_condition("서울", AT)["code"]


def test_mock_get_weather_condition_actual_follows_distribution():
    codes = [pe.mock_get_weather_condition(f"R{i}", AT)["code"] for i in range(3000)]
    for code, p in pe.weather_distribution(AT.month).items():
        assert abs(codes.count(code) / len(codes) - p) < 0.04, code


def test_weather_distribution_actual_more_snow_in_winter():
    assert pe.weather_distribution(1)["SNOW"] > pe.weather_distribution(7)["SNOW"]


# ---------------------------------------------------------------- 포아송 · 수급 · 서지
def test_poisson_smoke():
    assert isinstance(pe.poisson(3, random.Random(0)), int)
    assert pe.poisson(0, random.Random(0)) == 0


def test_poisson_actual_mean_close_to_lambda():
    rng = random.Random(1)
    assert abs(sum(pe.poisson(6, rng) for _ in range(4000)) / 4000 - 6) < 0.3


def test_market_smoke():
    m = pe.market("경기", at=AT)
    assert {"region", "demand", "supply", "ds_ratio", "surge", "surge_on", "rush_hour", "weather"} <= m.keys()
    assert m["supply"] >= 1 and 0 <= m["surge"] <= pe.SURGE_MAX


def test_market_actual_rush_hour_demand_higher_than_dawn():
    rush = sum(pe.market("경기", at=datetime(2026, 11, 17, 8, d))["ds_ratio"] for d in range(1, 29)) / 28
    dawn = sum(pe.market("경기", at=datetime(2026, 11, d, 3))["ds_ratio"] for d in range(1, 29)) / 28
    assert rush > dawn * 2


@pytest.mark.parametrize("ds, surge_positive", [(0.5, False), (1.5, False), (1.6, True), (3.0, True)])
def test_surge_multiplier_actual_threshold(ds, surge_positive):
    assert (pe.surge_multiplier(ds) > 0) is surge_positive


def test_surge_multiplier_actual_capped():
    assert pe.surge_multiplier(10) == pe.SURGE_MAX
    assert pe.surge_multiplier(2.0) <= pe.surge_multiplier(3.0) <= pe.SURGE_MAX


# ---------------------------------------------------------------- 통행료 · 오지 · 장재물 · 예약
def test_mock_toll_lookup_smoke():
    t = pe.mock_toll_lookup(11.0, 300)
    assert {"class", "highway_km", "amount", "note"} <= t.keys() and t["amount"] % 100 == 0


def test_mock_toll_lookup_actual_heavier_class_pays_more():
    assert pe.mock_toll_lookup(25.0, 300)["amount"] > pe.mock_toll_lookup(1.0, 300)["amount"] > 0
    assert pe.toll_class(1.0) < pe.toll_class(25.0) == 5


def test_mock_toll_lookup_actual_short_trip_free():
    assert pe.mock_toll_lookup(11.0, 15)["amount"] == 0


@pytest.mark.parametrize("region, hub_km, rate", [("강원", 30, 0.10), ("경북", 80, 0.10), ("경기", 10, 0.0)])
def test_remote_surcharge_actual(region, hub_km, rate):
    assert pe.remote_surcharge(region, hub_km)["rate"] == rate


@pytest.mark.parametrize("item_mm, truck_mm, rate", [(1200, 9100, 0.0), (6200, 9100, 0.10), (9500, 10100, 0.20),
                                                     (4000, 4300, 0.10), (None, 9100, 0.0)])
def test_long_cargo_surcharge_actual(item_mm, truck_mm, rate):
    assert pe.long_cargo_surcharge(item_mm, truck_mm)["rate"] == rate


def test_reservation_smoke():
    r = pe.reservation(None, AT)
    assert r["reserved"] is False and r["discount"] == 0.0


def test_reservation_actual_early_booking_discount():
    now = datetime(2026, 11, 17, 10)
    assert pe.reservation(now + timedelta(hours=30), now)["discount"] == 0.05
    assert pe.reservation(now + timedelta(hours=3), now)["discount"] == 0.0
    assert not pe.reservation(now - timedelta(hours=1), now)["reserved"]
