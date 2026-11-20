"""운임 · 정산 서비스 (services/billing_service) 단위 테스트 — AI 적정 운임 · 중량 할증 · 모의 PG."""

from datetime import datetime, timedelta

import pytest

from src.engines import pricing_engine as pe
from src.engines.matcher import spatial as geo
from src.services import billing_service as billing

ORIGIN, DEST = "경기 평택항 물류단지", "충남 아산시 탕정면"
HOT = {"region": "경기", "demand": 12, "supply": 4, "ds_ratio": 3.0, "surge": pe.surge_multiplier(3.0),
       "surge_on": True, "rush_hour": True, "real_demand": 0, "real_supply": 0,
       "weather": {"region": "경기", "code": "SNOW", "label": "폭설", "multiplier": 0.30}}
CALM = dict(HOT, ds_ratio=1.0, surge=0.0, surge_on=False, rush_hour=False,
            weather={"region": "경기", "code": "CLEAR", "label": "맑음", "multiplier": 0.0})


def _factors(e: dict) -> dict:
    return {f["key"]: f["amount"] for f in e["factors"]}


# ---------------------------------------------------------------- 표 · 상수
def test_ctor_truck_fare_table():
    assert set(billing.TRUCK_TYPES) >= {"1톤 카고", "11톤 탑차", "25톤 무진동"}
    assert billing.COLD_TYPES <= set(billing.TRUCK_TYPES)
    assert billing.VAT_RATE == 0.10


# ---------------------------------------------------------------- estimate
def test_estimate_smoke():
    e = billing.estimate("11톤 탑차", ORIGIN, DEST, market=CALM)
    assert {"distance_km", "base", "factors", "recommended", "band", "market", "toll"} <= e.keys()
    assert e["recommended"] > 0 and e["recommended"] % 5_000 == 0
    assert e["band"][0] <= e["recommended"] <= e["band"][1]
    assert {f["key"] for f in e["factors"]} == {"weight", "weather", "surge", "urgent", "wait", "work", "toll",
                                                "remote", "long", "reservation"}


def test_estimate_actual_distance_is_road_km():
    assert billing.estimate("11톤 탑차", ORIGIN, DEST, market=CALM)["distance_km"] == geo.road_km(ORIGIN, DEST)


def test_estimate_actual_heavier_cargo_costs_more():
    light = billing.estimate("11톤 탑차", ORIGIN, DEST, weight_kg=1_000, market=CALM)
    heavy = billing.estimate("11톤 탑차", ORIGIN, DEST, weight_kg=10_500, market=CALM)
    assert heavy["recommended"] > light["recommended"]


def test_estimate_actual_snow_and_surge_multiply_base():
    e = billing.estimate("11톤 탑차", "경기 평택시", "부산항", market=HOT)
    f = _factors(e)
    assert abs(f["weather"] - e["base"] * 0.30) <= 5_000
    assert abs(f["surge"] - e["base"] * HOT["surge"]) <= 5_000
    assert e["recommended"] > billing.estimate("11톤 탑차", "경기 평택시", "부산항", market=CALM)["recommended"]


def test_estimate_actual_conditions_add_fixed_fees():
    base = _factors(billing.estimate("11톤 탑차", ORIGIN, DEST, market=CALM, wait_minutes=0))
    extra = _factors(billing.estimate("11톤 탑차", ORIGIN, DEST, market=CALM, urgent=True, cold_chain=True,
                                      wait_minutes=60))
    assert base["urgent"] == 0 and extra["urgent"] == 20_000
    assert base["work"] == 0 and extra["work"] == 30_000
    assert base["wait"] == 0 and extra["wait"] == 20_000


def test_estimate_actual_toll_and_remote_surcharge():
    f = _factors(billing.estimate("11톤 탑차", "경기 평택시", "강원 원주시", market=CALM))
    assert f["toll"] > 0 and f["remote"] > 0


def test_estimate_actual_long_cargo_and_early_booking():
    f = _factors(billing.estimate("25톤 무진동", "울산 남구", "경기 파주시", market=CALM, max_item_mm=9500,
                                  pickup_at=datetime.now() + timedelta(days=2)))
    assert f["long"] > 0 and f["reservation"] < 0


def test_estimate_actual_min_fare_for_short_trip():
    e = billing.estimate("1톤 카고", "경기 평택시", "경기 평택시", market=CALM)
    assert e["base"] >= billing.TRUCK_TYPES["1톤 카고"][0]


# ---------------------------------------------------------------- weight_surcharge / price_gap_ratio
def test_weight_surcharge_smoke():
    amount, detail = billing.weight_surcharge(500_000, "11톤 탑차", 5_000)
    assert isinstance(amount, int) and isinstance(detail, str)


@pytest.mark.parametrize("kg, amount", [(None, 0), (5_500, 0), (11_000, 50_000)])
def test_weight_surcharge_actual(kg, amount):
    # 최대 적재량 50% 까지 0원 → 100% 에서 기본 운임의 +10%
    assert billing.weight_surcharge(500_000, "11톤 탑차", kg)[0] == amount


@pytest.mark.parametrize("offered, rec, gap", [(90_000, 100_000, 0.1), (100_000, 100_000, 0.0),
                                               (120_000, 100_000, -0.2), (10, 0, 0.0)])
def test_price_gap_ratio_actual(offered, rec, gap):
    assert billing.price_gap_ratio(offered, rec) == pytest.approx(gap)
