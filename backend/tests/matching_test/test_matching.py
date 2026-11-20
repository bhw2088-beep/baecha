"""
자동 매칭 · 도로 거리 · 운임 점검 (서버 없이, 시드 데이터로).   실행: backend 폴더에서
    python -m tests.matching_test.est_matching

- 주소 → 좌표 (단어 앞부분 기준: '구미 국가산업단지' 가 '가산' 으로 잡히지 않는지)
- 도로 경로 캐시 · 캐시에 없는 지점은 가까운 캐시 지점 경유 도로 거리
- 매칭 점수 · 배차 제안 → 거절 → 다음 순위 · AI 추천 리스트 직접 수락
- 관제 매칭 설정 변경 즉시 적용 · AI 적정 운임 (중량 할증)
"""

from tests._check import check, finish, section

from src.engines.matcher import spatial as geo
from src.services import billing_service as pricing
from src.orm.database import Store
from src.utils.errors import ApiError
from src.services.matching_service import match_score

store = Store()  # 시드 데이터로 새로 만든 저장소 (실행 중인 서버와 무관)

section("주소 → 좌표")
inv = {v: k for k, v in geo.REGION_COORDS.items()}
for addr, key in [("경북 구미 국가산업단지", "구미"), ("서울 금천구 가산동", "가산"), ("경기 평택항 물류센터", "평택항"),
                  ("대전 유성구 대덕연구단지", "대덕"), ("인천항 물류센터", "인천항"), ("부산항", "부산항")]:
    got = inv.get(geo.geocode(addr))
    check(f"{addr} → {key}", geo.geocode(addr) == geo.REGION_COORDS[key], f"결과 {got}")
check("드롭다운 지역 40곳 이상", len(geo.region_options()) >= 40, f"{len(geo.region_options())}곳")
check("목록 밖 주소 판별", not geo.is_known("제주 서귀포시"))

section("도로 경로")
check("경로 캐시 로드", geo.route_count() >= 900, f"{geo.route_count()}개 조합")
nodes = {geo.coord_key(n) for n in geo._nodes}
missing = [r for r in geo.region_options() if geo.coord_key(geo.geocode(r)) not in nodes]
check("드롭다운 지역은 모두 캐시에 있음", not missing, ", ".join(missing))
a, b = geo.geocode("서울 금천구 가산동"), geo.geocode("부산 사상구 감전동")
km, straight = geo.route_km(a, b), geo.haversine_km(a, b)
check("도로 거리 > 직선 거리", km > straight, f"도로 {km}km · 직선 {straight:.0f}km")
check("도로 경로는 여러 지점", len(geo.route(a, b)) > 10, f"{len(geo.route(a, b))}개 지점")
p = (36.80, 127.20)  # 캐시에 없는 임의 지점 (운행 중 차량 위치 같은)
far = geo.route_km(p, b)
check("캐시 밖 지점도 도로 경로 경유", len(geo.route(p, b)) > 10 and far > geo.haversine_km(p, b),
      f"{far}km · {len(geo.route(p, b))}개 지점")

section("매칭 점수")
check("가깝고 꽉 채우면 1점", match_score(0, 1.0) == 1.0)
check("반경 끝 · 적재 0 이면 0점", match_score(50, 0.0) == 0.0)
check("가중치 반영", match_score(25, 0.5, radius_km=50, w_dist=1.0) == 0.5)

section("배차 제안 → 거절 → 다음 순위")
offered = [o for o in store.orders.values() if o["status"] == "MATCHING" and o["offer"]]
check("시드 데이터에 진행 중인 제안이 있음", bool(offered), f"{len(offered)}건")
if offered:
    o = offered[0]
    first = o["offer"]["driver_id"]
    store.decline_offer(first, o["id"])
    check("거절한 기사는 거절 목록에", first in o["declined"])
    nxt = o["offer"]["driver_id"] if o["offer"] else None
    check("다음 제안은 다른 기사에게 (또는 매칭 실패)", nxt != first,
          f"다음 {nxt}" if nxt else f"실패 사유 {o['match_fail']}")
    try:
        store.accept_offer(first, o["id"])
        check("거절한 제안은 수락 불가", False)
    except ApiError:
        check("거절한 제안은 수락 불가", True)

section("AI 추천 리스트 직접 수락")
picked = None
for d in store.drivers.values():
    if store._active_run_of(d["id"]):
        continue
    recs = [r for r in store.driver_recommendations(d["id"]) if not r["rec"]["offered_to_other"]]
    if recs:
        picked = (d["id"], recs[0])
        break
check("추천 리스트를 받을 수 있는 공차 기사가 있음", picked is not None)
if picked:
    driver_id, rec = picked
    check("추천 항목에 점수 · 상차 거리 · 적정 운임", all(k in rec["rec"] for k in ("score", "pickup_km"))
          and rec["fare_est"]["recommended"] > 0)
    v = store.accept_pick(driver_id, rec["id"])
    check("직접 수락 → 배차 완료", v["status"] == "ASSIGNED" and v["driver"]["id"] == driver_id,
          v["match"]["by_label"])

section("관제 매칭 설정")
before = dict(store.settings)
view = store.update_settings({"radius_km": 150, "timeout_sec": 30, "w_dist": 0.3}, by="테스트")
check("설정 저장", store.settings["radius_km"] == 150 and view["formula"] == "거리 30% + 적재율 70%", view["formula"])
check("변경 이력 기록", view["log"] and "탐색 반경 50km → 150km" in view["log"][0]["changes"])
new_offers = [o for o in store.orders.values() if o["offer"] and o["offer"]["timeout"] == 30]
check("새로 보낸 제안은 새 제한시간", all(o["offer"]["timeout"] == 30 for o in new_offers), f"{len(new_offers)}건")
store.update_settings({k: before[k] for k in ("radius_km", "timeout_sec", "w_dist")}, by="테스트")
check("기본값으로 되돌림", store.settings == before)

section("AI 적정 운임")
light = pricing.estimate("11톤 탑차", "경기 평택항 물류단지", "충남 아산시 탕정면", weight_kg=1_000)
heavy = pricing.estimate("11톤 탑차", "경기 평택항 물류단지", "충남 아산시 탕정면", weight_kg=10_500)
check("거리는 도로 기준", light["distance_km"] == geo.road_km("경기 평택항 물류단지", "충남 아산시 탕정면"),
      f"{light['distance_km']}km")
check("무거우면 중량 할증", heavy["recommended"] > light["recommended"],
      f"{light['recommended']:,} → {heavy['recommended']:,}원")
o = next(iter(store.orders.values()))
fe = store.order_view(o)["fare_est"]
check("화물마다 적정 운임 · 차이 비율", {"recommended", "gap_ratio", "fare_per_km"} <= fe.keys(),
      f"{o['id']} 제시 {fe['fare']:,} · 적정 {fe['recommended']:,}")

section("주소 검증")
try:
    store.create_order("S-001", {"title": "검증", "truck_type": "1톤 카고", "origin": "제주 서귀포시",
                                 "destination": "부산항", "price": 100_000})
    check("목록 밖 상차지는 등록 거부", False)
except ApiError as e:
    check("목록 밖 상차지는 등록 거부", e.status == 400, e.detail)

section("공차 거리 비교: 선택지가 없는 라운드 제외")
from datetime import datetime as _dt  # noqa: E402
st3 = Store()
st3.metrics_log.clear()
o1, o2 = {"id": "X-1"}, {"id": "X-2"}
# 화물 1건 × 후보 1명: 어떤 방식이든 같은 기사 → 비교에서 제외
st3._log_round(_dt.now(), [o1], {"X-1": []}, {"X-1": [{"id": "D-A", "pickup_km": 28.4}]},
               [("X-1", "D-A", 0.8)], [("X-1", "D-A", 0.8)], 5.0)
e = st3.metrics_log[-1]
check("후보 1명뿐 → 선택지 없음 · 단축률 기록 안 함", e["has_choice"] is False and e["deadhead_vs_nearest_pct"] is None)
# 화물 2건 × 기사 2명 (D-A 가 두 화물 모두의 후보) → 선택지 있음
rk = {"X-1": [{"id": "D-A", "pickup_km": 5.0}, {"id": "D-B", "pickup_km": 9.0}],
      "X-2": [{"id": "D-A", "pickup_km": 2.0}]}
st3._log_round(_dt.now(), [o1, o2], {"X-1": [], "X-2": []}, rk,
               [("X-1", "D-B", 0.7), ("X-2", "D-A", 0.9)], [("X-1", "D-A", 0.9)], 5.0)
e = st3.metrics_log[-1]
check("후보가 겹치는 라운드 → 선택지 있음 · 단축률 기록", e["has_choice"] is True and e["deadhead_vs_nearest_pct"] is not None,
      f"헝가리안 {e['hungarian_avg_km']}km vs 최단거리 {e['nearest_avg_km']}km → {e['deadhead_vs_nearest_pct']}%")
m = st3.matching_metrics()
check("집계는 선택지 있던 라운드만", m["comparable_rounds"] == 1, f"{m['comparable_rounds']}회")

finish()
