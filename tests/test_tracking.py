"""
실시간 추적 · 감사 로그 해시 체인 · 리뷰 점검 (서버 없이).   실행: 수정본 폴더에서  python tests/test_tracking.py

- 경로 거리 계산(Shapely 또는 순수 파이썬) · 디바운싱(정확히 3번째 경고, 복귀 시 초기화)
- 핑 처리: 지터는 정상, 단발 튐은 무시, 연속 이탈 → 이상 감지 + 감사 로그, 배정 기사가 아닌 단말 → 재위탁 의심
- 지연 감지: Delay Delta 30분 초과 → 알림
- 해시 체인: 정상 검증 · 한 줄 수정/삭제 시 위변조 감지
- 리뷰: 완료 화물만 · 중복 금지 · 3건 평균 3점 미만 → 자동 블랙리스트
"""

import random
from datetime import datetime, timedelta

from _check import check, finish, section

from backend.config import settings
from backend.core.database import Store
from backend.core.errors import ApiError
from backend.engines import tracking_engine as te
from backend.engines.fsm.order_fsm import verify_chain
from backend.engines.matcher.spatial import along

section(f"경로 거리 · 디바운싱 (엔진: {'Shapely' if te.HAS_SHAPELY else '순수 파이썬'})")
route = [(37.0, 127.0), (37.0, 127.1), (37.1, 127.1)]
check("경로 위 점 거리 ≈ 0", te.locate((37.0, 127.05), route)["distance_km"] < 0.01)
d = te.locate((37.018, 127.05), route)["distance_km"]
check("2km 떨어진 점 ≈ 2km", 1.9 < d < 2.1, f"{d:.3f}km")
check("진행률 0~1", 0.2 < te.locate((37.0, 127.05), route)["progress"] < 0.35)
c, events = 0, []
for off in [True, True, True, True, False]:
    c, ev = te.debounce(c, off, 3)
    events.append(ev)
check("정확히 3번째에 ALERT 한 번 · 복귀 시 RECOVERED", events == [None, None, "ALERT", None, "RECOVERED"], str(events))
c, ev1 = te.debounce(0, True, 3)
c, ev2 = te.debounce(c, False, 3)
check("한 번 튀고 복귀하면 경고 없음", ev1 is None and ev2 is None)
check("Delay Delta: 계획보다 40분 늦으면 +40", te.delay_minutes(100, 70, 120) == 40)

section("핑 처리")
store = Store()
rng = random.Random(3)
o = next(o for o in store.orders.values() if o["status"] == "IN_TRANSIT" and not o["deviated"]
         and not store._open_anomaly(o["id"]))
path = store._path(o)
before_audit = len(store.audit)


def ping(p, driver=None):
    return store.ingest_ping({"order_id": o["id"], "driver_id": driver or o["driver_id"], "lat": p[0], "lon": p[1],
                              "speed": 80, "source": "test"})


base = o["progress"] / 100
for i in range(5):
    r = ping(te.jitter(along(path, base + i * 0.002)[0], rng))
check("지터 8m 핑 → 경로 위 · 경고 없음", r["applied"] and r["off_count"] == 0, f"거리 {r['distance_km']}km")
check("서버가 위경도로 진행률 계산", store.orders[o["id"]]["progress"] >= base * 100)
ping(te.offset_point(path, base + 0.012, 3.0))
r = ping(along(path, base + 0.013)[0])
check("단발 GPS 튐 → 경고 없음", store.tracking[o["id"]]["alerts"] == 0 and not store._open_anomaly(o["id"], "DEVIATION"))
res = [ping(te.offset_point(path, base + 0.014 + i * 0.001, 2.0)) for i in range(4)]
check("연속 이탈: 1·2번째 경고 없음, 3번째 ALERT", [x["event"] for x in res[:3]] == [None, None, "ALERT"])
check("이상 감지(DEVIATION) 생성 · deviated 표시", bool(store._open_anomaly(o["id"], "DEVIATION")) and o["deviated"])
check("이탈 감사 로그", any(e["event"] == "deviation" and e["order_id"] == o["id"] for e in store.audit[before_audit:]))
ping(along(path, base + 0.02)[0])
check("경로 복귀 → deviated 해제", not o["deviated"])

r = ping(along(path, base + 0.02)[0], driver="D-002")
check("다른 기사 단말 핑 → 반영 안 함", not r["applied"])
check("재위탁 의심 이상 감지 (HIGH)", (store._open_anomaly(o["id"], "SUSPICIOUS_REOUTSOURCING") or {}).get("severity") == "HIGH")
try:
    store.check_ping(o["id"], "D-003")
    check("API 앞단 검증: 403", False)
except ApiError as e:
    check("API 앞단 검증: 403", e.status == 403)

st = store.tracking[o["id"]]
st["anchor"] = datetime.now() - timedelta(minutes=10)  # 배속 20 → 운행 시간 200분 경과, 진행은 거의 없음
r = ping(along(path, o["progress"] / 100)[0])
check("Delay Delta 30분 초과 → 지연 알림", r["delay_minutes"] > 30 and bool(store._open_anomaly(o["id"], "DELAY")),
      f"+{r['delay_minutes']}분")

section("계획 경로 planned_polyline · 관제 상세 패널")
from backend.engines.matcher import spatial as geo  # noqa: E402
pts = [(37.12345, 127.54321), (36.5, 127.0), (35.1, 129.04)]
check("폴리라인 인코딩 ↔ 디코딩 왕복", geo.decode_polyline(geo.encode_polyline(pts)) == pts)
check("모든 화물에 planned_polyline 저장", all(x.get("planned_polyline") for x in store.orders.values()))
raw = geo.route(geo.geocode(o["origin"]), geo.geocode(o["destination"]))
check("저장된 경로 = 도로 경로", store._path(o) == [tuple(p) for p in raw], f"{len(raw)}개 지점")
o["planned_polyline"] = geo.encode_polyline([raw[0], raw[-1]])  # 계획 경로를 직선으로 바꾸면
far = te.locate(raw[len(raw) // 2], store._path(o))["distance_km"]
check("이탈 판정은 저장된 계획 경로 기준", far > 0.5, f"도로 중간 지점 ↔ 직선 계획 {far:.1f}km")
store._plan_route(o)
det = store.admin_driver_detail(o["driver_id"])
check("상세 패널: 기사 · 운행 · 감사 로그 · 리뷰", det["driver"]["phone"] and det["run"]["id"] == o["id"]
      and det["audit"] and det["run"]["planned_points"] == len(raw) and "reviews" in det)
try:
    store.admin_driver_detail("D-999")
    check("없는 기사 → 404", False)
except ApiError as e:
    check("없는 기사 → 404", e.status == 404)

section("감사 로그 SHA-256 해시 체인")
v = verify_chain(store.audit)
check("전체 체인 정상", v["ok"], f"{v['checked']}줄")
tampered = [dict(e) for e in store.audit]
tampered[5]["amount"] = (tampered[5]["amount"] or 0) + 1
v = verify_chain(tampered)
check("금액 한 줄 수정 → 감지", not v["ok"] and v["broken_at"] == tampered[5]["id"], v["reason"])
deleted = [dict(e) for e in store.audit]
del deleted[10]
check("한 줄 삭제 → 감지", not verify_chain(deleted)["ok"])

section("리뷰 · 블랙리스트")
done = next(x for x in store.orders.values() if x["status"] == "DELIVERED" and x["driver_id"] == "D-003")
shipper = {"role": "shipper", "org_id": done["shipper_id"]}
r = store.create_review(shipper, done["id"], 2, ["지연 도착"], "늦음")
check("완료 화물 평가 등록", r["stars"] == 2 and r["target"])
try:
    store.create_review(shipper, done["id"], 5, [])
    check("중복 평가 금지", False)
except ApiError as e:
    check("중복 평가 금지", e.status == 409)
running = next(x for x in store.orders.values() if x["status"] == "IN_TRANSIT")
try:
    store.create_review({"role": "shipper", "org_id": running["shipper_id"]}, running["id"], 5, [])
    check("운행 중 화물 평가 금지", False)
except ApiError as e:
    check("운행 중 화물 평가 금지", e.status == 409)
d7 = store.drivers["D-007"]
check("D-007 리뷰 2건 → 아직 제한 아님", not d7.get("blacklisted"))
store.reviews["REV-HIST-9-S"] = {"id": "REV-HIST-9-S", "order_id": "HIST-9", "from_role": "shipper", "from_id": "S-001",
                                 "to_role": "driver", "to_id": "D-007", "stars": 1, "tags": [], "comment": "",
                                 "at": datetime.now()}
store._auto_blacklist("D-007", datetime.now())
check("3건째 평균 3점 미만 → 자동 이용 제한", d7.get("blacklisted") is True, store.driver_rating("D-007")["avg"])
check("이용 제한 기사는 배차 불가", not store.driver_dispatchable(d7))

section("데모 자동 하차 완료 (DEMO_AUTO_DELIVER)")
st2 = Store()
run = next(o for o in st2.orders.values() if o["status"] == "IN_TRANSIT" and not st2._open_anomaly(o["id"], "STAGNATION"))
run["progress"] = 98.0
st2.tick()
check("스위치 꺼짐 → 자동 완료 안 함", run["status"] == "IN_TRANSIT")
settings.demo_auto_deliver = True
try:
    st2.tick()
finally:
    settings.demo_auto_deliver = False
drv = st2.drivers[run["driver_id"]]
check("도착(97% 이상) → 자동 하차 완료 · 인수증", run["status"] == "DELIVERED" and run["pod"]["photo"].get("auto"),
      run["pod"]["hash"])
check("기사 위치 = 방금 하차지 (다음 배차 출발점)", drv["location"] == run["destination"], drv["location"])
check("기사가 다시 공차 (운행 없음)", st2._active_run_of(drv["id"]) is None)

finish()
