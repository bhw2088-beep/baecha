"""실시간 추적 엔진 (engines/tracking_engine) 단위 테스트 — 경로 거리 · 디바운싱 · 지연 · 모의 GPS."""

import random

import pytest

from src.engines import tracking_engine as te
from src.engines.matcher.spatial import along

ROUTE = [(37.0, 127.0), (37.0, 127.1), (37.1, 127.1)]


# ---------------------------------------------------------------- 상수
def test_ctor_engine_flag():
    assert isinstance(te.HAS_SHAPELY, bool)


# ---------------------------------------------------------------- locate
def test_locate_smoke():
    r = te.locate((37.0, 127.05), ROUTE)
    assert {"distance_km", "progress", "engine"} <= r.keys()
    assert 0 <= r["progress"] <= 1


def test_locate_actual_point_on_route_is_zero_distance():
    assert te.locate((37.0, 127.05), ROUTE)["distance_km"] < 0.01


def test_locate_actual_two_km_off_route():
    assert 1.9 < te.locate((37.018, 127.05), ROUTE)["distance_km"] < 2.1


def test_locate_actual_progress_along_route():
    assert 0.2 < te.locate((37.0, 127.05), ROUTE)["progress"] < 0.35
    assert te.locate(ROUTE[-1], ROUTE)["progress"] > 0.99


def test_locate_actual_degenerate_route():
    assert te.locate((37.0, 127.0), [(37.0, 127.0)])["distance_km"] == 0.0


# ---------------------------------------------------------------- debounce
def test_debounce_smoke():
    assert te.debounce(0, True, 3) == (1, None)


def test_debounce_actual_alert_exactly_on_third_hit_then_recover():
    c, events = 0, []
    for off in [True, True, True, True, False]:
        c, ev = te.debounce(c, off, 3)
        events.append(ev)
    assert events == [None, None, "ALERT", None, "RECOVERED"]


def test_debounce_actual_single_spike_is_ignored():
    c, ev1 = te.debounce(0, True, 3)
    c, ev2 = te.debounce(c, False, 3)
    assert ev1 is None and ev2 is None and c == 0


# ---------------------------------------------------------------- delay_minutes
def test_delay_minutes_smoke():
    assert isinstance(te.delay_minutes(0, 0, 0), int)


@pytest.mark.parametrize("elapsed, remain_km, planned, delay", [(100, 70, 120, 40), (0, 0, 0, 0)])
def test_delay_minutes_actual(elapsed, remain_km, planned, delay):
    assert te.delay_minutes(elapsed, remain_km, planned) == delay


# ---------------------------------------------------------------- 모의 GPS (jitter · offset_point)
def test_jitter_smoke():
    p = te.jitter((37.0, 127.0), random.Random(1))
    assert isinstance(p, tuple) and len(p) == 2


def test_jitter_actual_stays_within_tens_of_meters():
    rng = random.Random(3)
    worst = max(te.locate(te.jitter(along(ROUTE, 0.3)[0], rng), ROUTE)["distance_km"] for _ in range(200))
    assert worst < 0.05


def test_offset_point_actual_is_requested_distance_off_route():
    p = te.offset_point(ROUTE, 0.25, 2.0)
    assert 1.8 < te.locate(p, ROUTE)["distance_km"] < 2.2
