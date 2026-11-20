"""
메모리 저장소 (orm/database.Store) — 자동 매칭 단위 테스트.

배차 제안 → 거절 → 다음 순위 · AI 추천 리스트 직접 수락 · 관제 매칭 설정 · 적정 운임 · 주소 검증 · 공차 거리 집계.
"""

from datetime import datetime

import pytest

from src.utils.errors import ApiError


def _pickable(store):
    for d in store.drivers.values():
        if store._active_run_of(d["id"]):
            continue
        recs = [r for r in store.driver_recommendations(d["id"]) if not r["rec"]["offered_to_other"]]
        if recs:
            return d["id"], recs[0]
    return None


# ---------------------------------------------------------------- 생성자 (기본 설정)
def test_ctor_default_matching_settings(seed_store):
    s = seed_store.settings
    assert {"radius_km", "timeout_sec", "w_dist"} <= s.keys()
    assert 0 <= s["w_dist"] <= 1 and s["radius_km"] > 0


# ---------------------------------------------------------------- 제안 · 거절
def test_decline_offer_smoke(store):
    o = next(o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"])
    first = o["offer"]["driver_id"]
    store.decline_offer(first, o["id"])
    assert first in o["declined"]


def test_decline_offer_actual_next_offer_goes_to_other_driver(store):
    o = next(o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"])
    first = o["offer"]["driver_id"]
    store.decline_offer(first, o["id"])
    nxt = o["offer"]["driver_id"] if o["offer"] else None
    assert nxt != first, f"실패 사유 {o.get('match_fail')}"
    with pytest.raises(ApiError):
        store.accept_offer(first, o["id"])


# ---------------------------------------------------------------- AI 추천 리스트
def test_driver_recommendations_smoke(store):
    picked = _pickable(store)
    assert picked is not None, "추천 리스트를 받을 수 있는 공차 기사가 시드에 있어야 함"
    _, rec = picked
    assert {"score", "pickup_km"} <= rec["rec"].keys() and rec["fare_est"]["recommended"] > 0


def test_accept_pick_actual_assigns_driver(store):
    driver_id, rec = _pickable(store)
    v = store.accept_pick(driver_id, rec["id"])
    assert v["status"] == "ASSIGNED" and v["driver"]["id"] == driver_id


# ---------------------------------------------------------------- 관제 매칭 설정
def test_update_settings_smoke(store):
    view = store.update_settings({"radius_km": 150, "timeout_sec": 30, "w_dist": 0.3}, by="테스트")
    assert store.settings["radius_km"] == 150
    assert view["formula"] == "거리 30% + 적재율 70%"


def test_update_settings_actual_logs_change_and_applies_to_new_offers(store):
    view = store.update_settings({"radius_km": 150, "timeout_sec": 30, "w_dist": 0.3}, by="테스트")
    assert view["log"] and "탐색 반경 50km → 150km" in view["log"][0]["changes"]
    new_offers = [o for o in store.orders.values() if o["offer"] and o["offer"]["timeout"] == 30]
    assert all(o["offer"]["timeout"] == 30 for o in new_offers)


def test_update_settings_actual_restore(store):
    before = dict(store.settings)
    store.update_settings({"radius_km": 150, "timeout_sec": 30, "w_dist": 0.3}, by="테스트")
    store.update_settings({k: before[k] for k in ("radius_km", "timeout_sec", "w_dist")}, by="테스트")
    assert store.settings == before


# ---------------------------------------------------------------- 운임 · 주소 검증
def test_order_view_actual_has_fare_estimate(seed_store):
    o = next(iter(seed_store.orders.values()))
    fe = seed_store.order_view(o)["fare_est"]
    assert {"recommended", "gap_ratio", "fare_per_km"} <= fe.keys()


def test_create_order_actual_rejects_unknown_origin(store):
    with pytest.raises(ApiError) as ex:
        store.create_order("S-001", {"title": "검증", "truck_type": "1톤 카고", "origin": "제주 서귀포시",
                                     "destination": "부산항", "price": 100_000})
    assert ex.value.status == 400


# ---------------------------------------------------------------- 공차 거리 집계 (헝가리안 vs 최단거리)
def test_log_round_actual_single_candidate_has_no_choice(store):
    store.metrics_log.clear()
    store._log_round(datetime.now(), [{"id": "X-1"}], {"X-1": []}, {"X-1": [{"id": "D-A", "pickup_km": 28.4}]},
                     [("X-1", "D-A", 0.8)], [("X-1", "D-A", 0.8)], 5.0)
    e = store.metrics_log[-1]
    assert e["has_choice"] is False and e["deadhead_vs_nearest_pct"] is None


def test_log_round_actual_overlapping_candidates_counted(store):
    store.metrics_log.clear()
    store._log_round(datetime.now(), [{"id": "X-1"}], {"X-1": []}, {"X-1": [{"id": "D-A", "pickup_km": 28.4}]},
                     [("X-1", "D-A", 0.8)], [("X-1", "D-A", 0.8)], 5.0)
    rk = {"X-1": [{"id": "D-A", "pickup_km": 5.0}, {"id": "D-B", "pickup_km": 9.0}],
          "X-2": [{"id": "D-A", "pickup_km": 2.0}]}
    store._log_round(datetime.now(), [{"id": "X-1"}, {"id": "X-2"}], {"X-1": [], "X-2": []}, rk,
                     [("X-1", "D-B", 0.7), ("X-2", "D-A", 0.9)], [("X-1", "D-A", 0.9)], 5.0)
    e = store.metrics_log[-1]
    assert e["has_choice"] is True and e["deadhead_vs_nearest_pct"] is not None
    assert store.matching_metrics()["comparable_rounds"] == 1
