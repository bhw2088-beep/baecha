"""공간 · 도로 경로 (engines/matcher/spatial) 단위 테스트 — 주소 → 좌표 · 도로 경로 캐시 · 폴리라인."""

import pytest

from src.engines.matcher import spatial as geo

SEOUL, BUSAN = "서울 금천구 가산동", "부산 사상구 감전동"


# ---------------------------------------------------------------- 데이터 로드
def test_ctor_region_tables_and_route_cache_loaded():
    assert len(geo.REGION_COORDS) >= 40 and len(geo.HUBS) >= 5
    assert geo.route_count() >= 900


def test_ctor_dropdown_regions_are_all_in_route_cache():
    nodes = {geo.coord_key(n) for n in geo._nodes}
    missing = [r for r in geo.region_options() if geo.coord_key(geo.geocode(r)) not in nodes]
    assert missing == []


# ---------------------------------------------------------------- geocode
def test_geocode_smoke():
    lat, lon = geo.geocode("경기 평택시")
    assert 33 < lat < 39 and 124 < lon < 132


@pytest.mark.parametrize("addr, key", [("경북 구미 국가산업단지", "구미"), ("서울 금천구 가산동", "가산"),
                                       ("경기 평택항 물류센터", "평택항"), ("대전 유성구 대덕연구단지", "대덕"),
                                       ("인천항 물류센터", "인천항"), ("부산항", "부산항")])
def test_geocode_actual_matches_word_prefix(addr, key):
    assert geo.geocode(addr) == geo.REGION_COORDS[key]


def test_is_known_actual():
    assert geo.is_known("경기 평택시") and not geo.is_known("제주 서귀포시")


# ---------------------------------------------------------------- 거리 · 경로
def test_haversine_km_actual_known_distance():
    assert geo.haversine_km((37.5665, 126.9780), (35.1796, 129.0756)) == pytest.approx(325, abs=10)
    assert geo.haversine_km((37.0, 127.0), (37.0, 127.0)) == 0


def test_route_smoke():
    pts = geo.route(geo.geocode(SEOUL), geo.geocode(BUSAN))
    assert len(pts) > 10 and all(len(p) == 2 for p in pts)


def test_route_km_actual_road_longer_than_straight_line():
    a, b = geo.geocode(SEOUL), geo.geocode(BUSAN)
    assert geo.route_km(a, b) > geo.haversine_km(a, b)


def test_route_actual_off_cache_point_goes_via_nearest_node():
    p, b = (36.80, 127.20), geo.geocode(BUSAN)
    assert len(geo.route(p, b)) > 10 and geo.route_km(p, b) > geo.haversine_km(p, b)


def test_road_km_actual_same_as_route_km():
    assert geo.road_km(SEOUL, BUSAN) == geo.route_km(geo.geocode(SEOUL), geo.geocode(BUSAN))


# ---------------------------------------------------------------- 폴리라인 · 보간
def test_encode_polyline_smoke():
    assert isinstance(geo.encode_polyline([(37.0, 127.0), (36.0, 128.0)]), str)


def test_polyline_actual_round_trip():
    pts = [(37.12345, 127.54321), (36.5, 127.0), (35.1, 129.04)]
    assert geo.decode_polyline(geo.encode_polyline(pts)) == pts


def test_along_actual_endpoints_and_middle():
    path = [(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)]
    assert geo.along(path, 0.0)[0] == pytest.approx((0.0, 0.0))
    assert geo.along(path, 1.0)[0] == pytest.approx((0.0, 2.0))
    assert geo.along(path, 0.5)[0] == pytest.approx((0.0, 1.0), abs=0.05)


def test_interpolate_actual():
    assert geo.interpolate((0.0, 0.0), (2.0, 4.0), 0.5) == pytest.approx((1.0, 2.0))
