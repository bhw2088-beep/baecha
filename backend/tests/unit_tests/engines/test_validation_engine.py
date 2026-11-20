"""적재 안전 규칙 (engines/validation_engine) 단위 테스트 — 안전율 · 축중량 · 필수 최소 톤수."""

import pytest

from src.engines import validation_engine as ve
from src.engines.packer.trucks import TRUCKS

ALL_TRUCKS = list(TRUCKS.values())


def _box(x, y, kg):
    return dict(x=x, y=y, z=0, length=1000, width=1000, height=1000, weight_kg=kg)


# ---------------------------------------------------------------- 상수 · 기본값
def test_ctor_constants():
    assert ve.FRONT_AXLE < 0 < ve.REAR_AXLE < 1
    assert 0 < ve.LIGHT_LOAD < 1
    assert ve.capacity_limit(0.05) == 0.95


# ---------------------------------------------------------------- safety_check
def test_safety_check_smoke():
    r = ve.safety_check(0.5, 0.5)
    assert {"status", "detail", "limit", "warn_at", "worst"} <= r.keys()
    assert r["limit"] == 0.95 and r["warn_at"] == 0.90


@pytest.mark.parametrize("vol, wt, status", [
    (0.88, 0.50, "pass"),
    (0.90, 0.50, "pass"),    # 경고 시작점 자체는 통과
    (0.92, 0.50, "warn"),    # 90% 초과 → 공간 부족 위험
    (0.95, 0.50, "warn"),    # 한도 95% 까지는 경고
    (0.50, 0.97, "fail"),    # 안전율 5% → 95% 초과 차단 (중량 쪽)
    (0.96, 0.10, "fail"),
])
def test_safety_check_actual_thresholds(vol, wt, status):
    assert ve.safety_check(vol, wt)["status"] == status


def test_safety_check_actual_margin_changes_limit():
    assert ve.safety_check(0.92, 0.0, safety_margin=0.10)["status"] == "fail"


# ---------------------------------------------------------------- axle_loads / axle_balance
def test_axle_loads_smoke():
    r = ve.axle_loads(1000, 0.5)
    assert abs(r["front_kg"] + r["rear_kg"] - 1000) < 0.2
    assert abs(r["front_ratio"] + r["rear_ratio"] - 1) < 0.002


def test_axle_loads_actual_cargo_behind_rear_axle_goes_to_rear():
    assert ve.axle_loads(1000, ve.REAR_AXLE)["rear_kg"] == 1000
    assert ve.axle_loads(1000, ve.FRONT_AXLE)["front_kg"] == 1000


def test_axle_balance_smoke():
    r = ve.axle_balance([_box(2500, 700, 4000)], 6000, 2400, 5000)
    assert {"status", "detail", "lateral", "longitudinal", "front_kg", "rear_kg"} <= r.keys()


def test_axle_balance_actual_centered_cargo_passes_and_loads_add_up():
    r = ve.axle_balance([_box(2500, 700, 4000)], 6000, 2400, 5000)
    assert r["status"] == "pass"
    assert abs(r["front_kg"] + r["rear_kg"] - 4000) < 1


def test_axle_balance_actual_heavy_cargo_in_front_corner_fails():
    assert ve.axle_balance([_box(0, 0, 4000)], 6000, 2400, 5000)["status"] == "fail"


def test_axle_balance_actual_light_cargo_is_relaxed_to_warn():
    r = ve.axle_balance([_box(0, 0, 300)], 6000, 2400, 5000)
    assert r["status"] == "warn" and r["light_load"] is True


def test_axle_balance_actual_empty_rows_pass():
    assert ve.axle_balance([], 6000, 2400, 5000)["status"] == "pass"


# ---------------------------------------------------------------- required_tonnage
def test_required_tonnage_smoke():
    assert isinstance(ve.required_tonnage(500, 1e9, ALL_TRUCKS), float)


@pytest.mark.parametrize("kg, cold, tons", [
    (950, False, 1.0),     # 1톤 × 95% = 950kg 까지
    (960, False, 2.5),     # 한도를 넘으면 한 단계 위
    (900, True, 5.0),      # 냉장 화물은 냉장 차량 중 가장 작은 것
    (23_000, False, 25.0),  # 25톤 × 95% = 23,750kg 까지
])
def test_required_tonnage_actual(kg, cold, tons):
    assert ve.required_tonnage(kg, 1e9, ALL_TRUCKS, cold_chain=cold) == tons


def test_required_tonnage_actual_none_when_nothing_fits():
    assert ve.required_tonnage(30_000, 1e9, ALL_TRUCKS) is None
