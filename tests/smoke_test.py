"""
API 전체 흐름 점검 (백엔드를 켠 상태에서).   실행: 수정본 폴더에서
    .venv\\Scripts\\python tests\\smoke_test.py  [API 주소, 기본 http://127.0.0.1:8000/api/v1]

화주 등록 → 자동 매칭 → 기사 수락(제안 또는 AI 추천) → 운행 → 전자인수증 → 청구 → 화주 승인,
그리고 관제 매칭 설정 변경 · 되돌리기까지 실제 API 로 한 바퀴 돈다.
※ 실행 중인 서버의 데모 데이터가 바뀝니다 (새 화물 1건 · 김철수 기사 운행 1건). 백엔드를 다시 켜면 초기화.
"""

import sys
import time

import requests
from _check import check, finish, section

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1"
PW = "baecha1234"


def call(method: str, path: str, token: str | None = None, expect: int = 200, **kw):
    r = requests.request(method, API + path, headers={"Authorization": f"Bearer {token}"} if token else {},
                         timeout=30, **kw)
    if r.status_code != expect:
        raise AssertionError(f"{method} {path} → {r.status_code} (기대 {expect}) {r.text[:200]}")
    return r.json()


def login(email: str) -> str:
    return call("POST", "/auth/login", json={"email": email, "password": PW})["token"]


section("서버 · 메타")
try:
    call("GET", "/health")
except requests.ConnectionError:
    print(f"  백엔드에 연결할 수 없습니다: {API}\n  docs/가이드/실행방법.md 의 [실행] 단계로 백엔드를 먼저 켜 주세요.")
    sys.exit(2)
meta = call("GET", "/meta")
check("지역 드롭다운 목록", len(meta["regions"]) >= 40, f"{len(meta['regions'])}곳")
check("매칭 설정 공개", "formula" in meta["matching"], meta["matching"]["formula"])

shipper, driver, admin = (login(e) for e in ("logistics@samsung.com", "driver.kim@baecha.co.kr",
                                              "admin.control@baecha.co.kr"))
check("데모 계정 3개 로그인", all((shipper, driver, admin)))

section("관제 매칭 설정")
cfg = call("GET", "/admin/matching/settings", admin)
orig = {k: cfg[k] for k in ("radius_km", "timeout_sec", "w_dist")}
new = call("PUT", "/admin/matching/settings", admin, json={"radius_km": 80, "timeout_sec": 90, "w_dist": 0.5})
check("설정 변경", new["radius_km"] == 80 and new["formula"] == "거리 50% + 적재율 50%", new["formula"])
call("PUT", "/admin/matching/settings", admin, json={"radius_km": 5, "timeout_sec": 60, "w_dist": 0.6}, expect=422)
check("범위 밖 값 거부 (반경 5km)", True)
call("PUT", "/admin/matching/settings", admin, json=orig)
check("원래 설정으로 복구", call("GET", "/admin/matching/settings", admin)["radius_km"] == orig["radius_km"])
call("PUT", "/admin/matching/settings", shipper, json=orig, expect=403)
check("화주는 설정 변경 불가", True)

section("화주 · 운임 · 등록")
sample = next(iter(call("GET", "/packing/meta", shipper)["samples"].values()))
plan = call("POST", "/packing/plan", shipper, json={"items": sample["items"], "truck_type": None,
                                                    "cold_chain": sample["cold_chain"]})
truck, weight = plan["truck"]["name"], plan["metrics"]["loaded_weight"]
check("3D 적재 시뮬레이션", not plan["unplaced"], f"{truck} · {weight:,.0f}kg")
try:
    job = call("POST", "/packing/plan-async", shipper, json={"items": sample["items"], "truck_type": None,
                                                              "cold_chain": sample["cold_chain"]}, expect=202)
except AssertionError as e:
    if "→ 503" not in str(e):
        raise
    job = None  # 도커 없이(메모리 모드) 실행 중 → Redis · Celery 워커 없음
    print("  [SKIP] Celery 워커 비동기 적재 시뮬레이션  — 작업 큐(Redis) 없음 · 도커로 실행하면 점검")
if job:
    res = {"status": "PENDING"}
    for _ in range(60):  # Celery 워커 결과 기다리기 (최대 약 30초)
        res = call("GET", f"/packing/tasks/{job['task_id']}", shipper)
        if res["status"] in ("SUCCESS", "FAILURE"):
            break
        time.sleep(0.5)
    check("Celery 워커 비동기 적재 시뮬레이션", res["status"] == "SUCCESS" and res.get("ok")
          and res["result"]["truck"]["name"] == truck,
          f"{res['status']} · {res.get('worker', '-')} · {res.get('seconds')}초")
origin, dest = "경기 평택항 물류단지", "충남 아산시 탕정면"
est = call("POST", "/pricing/estimate", shipper, json={"truck_type": truck, "origin": origin, "destination": dest,
                                                         "cold_chain": sample["cold_chain"], "weight_kg": weight})
check("AI 적정 운임 (도로거리 · 중량)", est["recommended"] > 0 and any(f["key"] == "weight" for f in est["factors"]),
      f"{est['recommended']:,}원 · {est['distance_km']}km")
call("POST", "/pricing/estimate", shipper, json={"truck_type": truck, "origin": "제주", "destination": dest}, expect=400)
check("목록 밖 지역 운임 요청 거부", True)
body = {"title": "스모크 테스트 화물", "truck_type": truck, "origin": origin, "destination": dest,
        "price": est["recommended"], "cold_chain": sample["cold_chain"], "items": sample["items"]}
call("POST", "/shipper/orders", shipper, json={**body, "origin": "아무데나 창고"}, expect=400)
check("목록 밖 상차지 등록 거부", True)
order = call("POST", "/shipper/orders", shipper, json=body, expect=201)
m = order["matching"]
check("등록 즉시 자동 매칭", m and m["state"] in ("OFFERED", "FAILED", "SEARCHING"),
      f"{order['id']} · {m['state']}" + (f" → {m['driver']['name']}" if m.get("driver") else ""))
check("화물에 도로 경로 · 적정 운임", len(order["route"]) > 2 and order["fare_est"]["recommended"] == est["recommended"])

section("기사 · 배차 제안 / AI 추천")
profile = call("GET", "/driver/profile", driver)
target = None
if profile["run"]:
    print(f"  (김철수 기사가 이미 운행 중 {profile['run']['id']} — 그 운행으로 이어서 점검)")
    target = profile["run"]["id"]
else:
    offer = call("GET", "/driver/offer", driver)
    recs = call("GET", "/driver/recommendations", driver)
    check("AI 추천 리스트 조회", isinstance(recs, list), f"{len(recs)}건")
    if recs:
        r0 = recs[0]
        check("추천 항목 구성", {"score", "pickup_km", "util"} <= r0["rec"].keys() and "fare_est" in r0,
              f"1순위 {r0['id']} · 점수 {r0['rec']['score'] * 100:.0f}")
    if offer:
        check("배차 제안 카드 정보", offer["matching"]["expires_in"] > 0 and "fare_est" in offer,
              f"{offer['id']} · 남은 {offer['matching']['expires_in']}초")
        target = call("POST", f"/driver/offer/{offer['id']}/accept", driver)["id"]
        check("제안 수락 → 배차 확정", True, target)
    else:
        free = [r for r in recs if not r["rec"]["offered_to_other"]]
        if free:
            target = call("POST", f"/driver/recommendations/{free[0]['id']}/accept", driver)["id"]
            check("AI 추천 직접 수락 → 배차 확정", True, target)
        else:
            check("수락할 수 있는 제안 · 추천 화물", False, "김철수 기사에게 지금 수락 가능한 화물이 없음")

if target:
    section("운행 → 인수증 → 청구 → 승인")
    run = call("GET", f"/orders/{target}", driver)
    if run["status"] == "ASSIGNED":
        run = call("POST", f"/driver/runs/{target}/start", driver)
    check("운행 시작", run["status"] == "IN_TRANSIT")
    lon, lat = run["route"][1]
    pong = call("POST", "/tracking/ping", driver, expect=202,
                json={"order_id": target, "lat": lat, "lon": lon, "speed": 80})
    check("GPS 핑 인입 (기사 토큰 · 배정 기사 대조)", pong["accepted"], pong["pipeline"])
    check("토큰 없는 핑 거부", requests.post(API + "/tracking/ping", json={"order_id": target, "lat": lat, "lon": lon},
                                         timeout=10).status_code == 401)
    call("POST", f"/driver/runs/{target}/complete", driver, expect=422, data={"receiver": "홍길동"})
    check("인수증 사진 없으면 하차 완료 거부", True)
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    done = call("POST", f"/driver/runs/{target}/complete", driver, data={"receiver": "홍길동"},
                files={"photo": ("pod.png", png, "image/png")})
    check("하차 완료 · 전자인수증 + 사진 SHA-256", done["status"] == "DELIVERED" and done["pod"]["hash"].startswith("0x")
          and len(done["pod"]["photo"]["sha256"]) == 64)
    r = requests.get(f"{API}/orders/{target}/pod/photo", headers={"Authorization": f"Bearer {admin}"}, timeout=10)
    check("관제가 인수증 사진 확인", r.status_code == 200 and r.content == png)
    call("POST", f"/driver/completed/{target}/invoice", driver, json={"extra_fee": 10_000, "toll_fee": 5_000})
    owner = call("GET", f"/orders/{target}", admin)["shipper"]
    if owner == "삼성물류":
        paid = call("POST", f"/shipper/history/{target}/approve-invoice", shipper)
        check("청구 → 화주 승인 → 정산 완료", paid["status"] == "SETTLED", f"{paid['total_fee']:,}원")
    else:
        check("청구서 발송", True, f"화주 {owner} (데모 화주 계정이 아니라 승인은 생략)")

    review = call("POST", "/reviews", shipper if owner == "삼성물류" else driver, expect=201,
                  json={"order_id": target, "stars": 5, "tags": ["시간 엄수"] if owner == "삼성물류" else ["상하차 신속"]})
    check("운송 완료 화물 상호 평가", review["stars"] == 5, review["id"])

section("컴플라이언스 · 감사 로그")
chk = call("GET", "/compliance/check", params={"biz_no": "123-45-67890", "plate": "부산 11가 2233"})
check("사업자 Mod-10 · 번호판 사전 확인", not chk["business"]["ok"] and chk["plate"]["plate_type"] == "PRIVATE_WHITE",
      f"{chk['business']['label']} · {chk['plate']['label']}")
r = requests.post(API + "/auth/register", timeout=10, data={
    "role": "shipper", "email": f"smoke{int(time.time())}@x.com", "password": "password1", "name": "스모크",
    "company": "스모크물류", "biz_no": "123-45-67890"})  # 검증 숫자 오류 → 국세청 키 유무와 상관없이 거절
check("가짜 사업자번호 가입 거절", r.status_code == 400, r.json().get("detail", "")[:60])
v = call("GET", "/admin/audit/verify", admin)
check("감사 로그 SHA-256 해시 체인 검증", v["ok"], f"{v['checked']}줄")
rep = call("GET", "/admin/reputation", admin)
check("평판 · 블랙리스트 보드", any(x["blacklisted"] for x in rep), f"제한 {sum(x['blacklisted'] for x in rep)}명")

section("관제")
s = call("GET", "/admin/summary", admin)
check("관제 요약", s["vehicles"] > 0, f"차량 {s['vehicles']}대 · 매칭 진행 {s['matching']}건 · 실패 {s['failed_matches']}건")
fleet = call("GET", "/admin/fleet", admin)
paths = [v for v in fleet if v["path"]]
check("운행 차량 도로 경로", paths and all(len(v["path"]) > 2 for v in paths), f"{len(paths)}대")
stats = call("GET", "/admin/matching", admin)
check("매칭 분석", stats["kpi"]["match_success_rate"] is not None, f"성공률 {stats['kpi']['match_success_rate']}%")
mm = call("GET", "/admin/matching/metrics", admin)
check("매칭 메트릭스 (p95 · 공차 단축 · 거절률)", mm["rounds"] > 0 and mm["ml_threshold"] == 0.75,
      f"p95 {mm['dispatch_latency_p95_sec']}초 · 라운드 {mm['rounds']}")
mk = call("GET", "/pricing/market", admin)
check("권역 수급 · 날씨", len(mk) >= 10 and all("ds_ratio" in x for x in mk))
tr = call("GET", "/admin/tracking", admin)
check("실시간 추적 판정", "runs" in tr, tr["engine"])
ans = call("POST", "/admin/agent/ask", admin, json={"text": "매칭 실패 보여줘"})
check("AI 관제 자연어 명령", bool(ans["answer"]), ans["engine"])

finish()
