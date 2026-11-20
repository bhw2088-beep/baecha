"""
메모리 저장소 (orm/database.Store) — GPS 핑 처리 · 이탈/재위탁/지연 감지 · 계획 경로 · 리뷰 · 자동 하차 단위 테스트.
"""

import random
from datetime import datetime, timedelta

import pytest

from src.config import settings
from src.engines import tracking_engine as te
from src.engines.matcher import spatial as geo
from src.engines.matcher.spatial import along
from src.utils.errors import ApiError


@pytest.fixture
def run(store):
    """운행 중이고 이상 감지가 없는 화물 하나 + 그 경로 · 핑 함수."""
    o = next(o for o in store.orders.values() if o["status"] == "IN_TRANSIT" and not o["deviated"]
             and not store._open_anomaly(o["id"]))
    path = store._path(o)

    def ping(p, driver=None):
        return store.ingest_ping({"order_id": o["id"], "driver_id": driver or o["driver_id"], "lat": p[0],
                                  "lon": p[1], "speed": 80, "source": "test"})
    return o, path, ping


# ---------------------------------------------------------------- 핑 처리
def test_ingest_ping_smoke(store, run):
    o, path, ping = run
    r = ping(along(path, o["progress"] / 100)[0])
    assert r["applied"] is True and {"distance_km", "off_count", "event"} <= r.keys()


def test_ingest_ping_actual_jitter_stays_on_route(store, run):
    o, path, ping = run
    base, rng = o["progress"] / 100, random.Random(3)
    for i in range(5):
        r = ping(te.jitter(along(path, base + i * 0.002)[0], rng))
    assert r["applied"] and r["off_count"] == 0
    assert store.orders[o["id"]]["progress"] >= base * 100


def test_ingest_ping_actual_single_spike_no_alert(store, run):
    o, path, ping = run
    base = o["progress"] / 100
    ping(te.offset_point(path, base + 0.012, 3.0))
    ping(along(path, base + 0.013)[0])
    assert store.tracking[o["id"]]["alerts"] == 0 and not store._open_anomaly(o["id"], "DEVIATION")


def test_ingest_ping_actual_three_consecutive_off_route_raises_deviation(store, run):
    o, path, ping = run
    base, before = o["progress"] / 100, len(store.audit)
    res = [ping(te.offset_point(path, base + 0.014 + i * 0.001, 2.0)) for i in range(4)]
    assert [x["event"] for x in res[:3]] == [None, None, "ALERT"]
    assert store._open_anomaly(o["id"], "DEVIATION") and o["deviated"]
    assert any(e["event"] == "deviation" and e["order_id"] == o["id"] for e in store.audit[before:])
    ping(along(path, base + 0.02)[0])
    assert not o["deviated"], "경로 복귀 → 해제"


def test_ingest_ping_actual_other_driver_device_is_reoutsourcing(store, run):
    o, path, ping = run
    r = ping(along(path, o["progress"] / 100 + 0.02)[0], driver="D-002")
    assert not r["applied"]
    assert (store._open_anomaly(o["id"], "SUSPICIOUS_REOUTSOURCING") or {}).get("severity") == "HIGH"


def test_check_ping_actual_wrong_driver_403(store, run):
    o, *_ = run
    with pytest.raises(ApiError) as ex:
        store.check_ping(o["id"], "D-003")
    assert ex.value.status == 403


def test_ingest_ping_actual_delay_over_30_minutes_alerts(store, run):
    o, path, ping = run
    ping(along(path, o["progress"] / 100)[0])   # 첫 핑으로 추적 상태를 만든 뒤
    store.tracking[o["id"]]["anchor"] = datetime.now() - timedelta(minutes=10)   # 배속 20 → 200분 경과, 진행 없음
    r = ping(along(path, o["progress"] / 100)[0])
    assert r["delay_minutes"] > 30 and store._open_anomaly(o["id"], "DELAY")


# ---------------------------------------------------------------- 계획 경로 · 관제 상세
def test_planned_polyline_actual_saved_for_all_orders(seed_store):
    assert all(x.get("planned_polyline") for x in seed_store.orders.values())


def test_path_actual_equals_road_route_and_drives_deviation(store, run):
    o, *_ = run
    raw = geo.route(geo.geocode(o["origin"]), geo.geocode(o["destination"]))
    assert store._path(o) == [tuple(p) for p in raw]
    o["planned_polyline"] = geo.encode_polyline([raw[0], raw[-1]])
    assert te.locate(raw[len(raw) // 2], store._path(o))["distance_km"] > 0.5


def test_admin_driver_detail_smoke(store, run):
    o, *_ = run
    det = store.admin_driver_detail(o["driver_id"])
    assert det["driver"]["phone"] and det["run"]["id"] == o["id"] and det["audit"] and "reviews" in det


def test_admin_driver_detail_actual_unknown_404(store):
    with pytest.raises(ApiError) as ex:
        store.admin_driver_detail("D-999")
    assert ex.value.status == 404


# ---------------------------------------------------------------- 리뷰 · 블랙리스트
def test_create_review_smoke(store):
    done = next(x for x in store.orders.values() if x["status"] == "DELIVERED" and x["driver_id"] == "D-003")
    r = store.create_review({"role": "shipper", "org_id": done["shipper_id"]}, done["id"], 2, ["지연 도착"], "늦음")
    assert r["stars"] == 2 and r["target"]


def test_create_review_actual_duplicate_and_running_rejected(store):
    done = next(x for x in store.orders.values() if x["status"] == "DELIVERED" and x["driver_id"] == "D-003")
    who = {"role": "shipper", "org_id": done["shipper_id"]}
    store.create_review(who, done["id"], 4, [])
    with pytest.raises(ApiError) as ex:
        store.create_review(who, done["id"], 5, [])
    assert ex.value.status == 409
    running = next(x for x in store.orders.values() if x["status"] == "IN_TRANSIT")
    with pytest.raises(ApiError) as ex:
        store.create_review({"role": "shipper", "org_id": running["shipper_id"]}, running["id"], 5, [])
    assert ex.value.status == 409


def test_auto_blacklist_actual_third_low_review_blocks_driver(store):
    d7 = store.drivers["D-007"]
    assert not d7.get("blacklisted"), "리뷰 2건 → 아직 제한 아님"
    store.reviews["REV-HIST-9-S"] = {"id": "REV-HIST-9-S", "order_id": "HIST-9", "from_role": "shipper",
                                     "from_id": "S-001", "to_role": "driver", "to_id": "D-007", "stars": 1,
                                     "tags": [], "comment": "", "at": datetime.now()}
    store._auto_blacklist("D-007", datetime.now())
    assert d7.get("blacklisted") is True and not store.driver_dispatchable(d7)


# ---------------------------------------------------------------- 데모 자동 하차 (DEMO_AUTO_DELIVER)
def test_tick_actual_auto_deliver_only_when_enabled(store, monkeypatch):
    o = next(x for x in store.orders.values() if x["status"] == "IN_TRANSIT"
             and not store._open_anomaly(x["id"], "STAGNATION"))
    o["progress"] = 98.0
    monkeypatch.setattr(settings, "demo_auto_deliver", False)
    store.tick()
    assert o["status"] == "IN_TRANSIT"
    monkeypatch.setattr(settings, "demo_auto_deliver", True)
    store.tick()
    drv = store.drivers[o["driver_id"]]
    assert o["status"] == "DELIVERED" and o["pod"]["photo"].get("auto")
    assert drv["location"] == o["destination"] and store._active_run_of(drv["id"]) is None
