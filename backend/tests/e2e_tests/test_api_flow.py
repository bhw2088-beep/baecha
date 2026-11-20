"""
API 전체 흐름 e2e — 화주 등록 → 자동 매칭 → 기사 수락 → 운행 · GPS 핑 → 인수증 → 청구 → 결제 → 리뷰,
그리고 권한 · 입력 검증 · 관제 화면 API.

흐름이 이어지는 테스트는 파일 순서대로 실행되고 STATE 로 값을 넘긴다 (앞 단계가 실패하면 뒤는 건너뜀).
"""

import time

import pytest

pytestmark = pytest.mark.e2e

ORIGIN, DEST = "경기 평택항 물류단지", "충남 아산시 탕정면"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
STATE: dict = {}


def need(*keys):
    missing = [k for k in keys if k not in STATE]
    if missing:
        pytest.skip(f"앞 단계 결과 없음: {missing}")
    return [STATE[k] for k in keys]


# ---------------------------------------------------------------- 서버 · 인증
def test_health_smoke(api):
    h = api.call("GET", "/health")
    assert h["ok"] is True and {"db", "redis"} <= h.keys()
    STATE["redis_ok"] = bool(h["redis"].get("ok"))


def test_meta_smoke(api):
    meta = api.call("GET", "/meta")
    assert len(meta["regions"]) >= 40 and "formula" in meta["matching"]


def test_login_actual_demo_accounts(tokens):
    assert all(tokens.values())


def test_login_actual_wrong_password_rejected(api):
    r = api.raw("POST", "/auth/login", json={"email": "logistics@samsung.com", "password": "wrong-password"})
    assert r.status_code in (400, 401)


def test_auth_actual_protected_api_requires_token(api):
    assert api.raw("GET", "/driver/profile").status_code == 401


def test_auth_actual_role_check(api, tokens):
    assert api.raw("GET", "/admin/matching/settings", tokens["shipper"]).status_code == 403
    assert api.raw("GET", "/driver/profile", tokens["shipper"]).status_code == 403


# ---------------------------------------------------------------- 관제 매칭 설정
def test_matching_settings_actual_update_validate_restore(api, tokens):
    admin = tokens["admin"]
    cfg = api.call("GET", "/admin/matching/settings", admin)
    orig = {k: cfg[k] for k in ("radius_km", "timeout_sec", "w_dist")}
    new = api.call("PUT", "/admin/matching/settings", admin, json={"radius_km": 80, "timeout_sec": 90, "w_dist": 0.5})
    assert new["radius_km"] == 80 and new["formula"] == "거리 50% + 적재율 50%"
    api.call("PUT", "/admin/matching/settings", admin, json={"radius_km": 5, "timeout_sec": 60, "w_dist": 0.6},
             expect=422)
    api.call("PUT", "/admin/matching/settings", admin, json=orig)
    assert api.call("GET", "/admin/matching/settings", admin)["radius_km"] == orig["radius_km"]


# ---------------------------------------------------------------- 화주: 적재 · 운임 · 등록
def test_packing_plan_smoke(api, tokens):
    sample = next(iter(api.call("GET", "/packing/meta", tokens["shipper"])["samples"].values()))
    plan = api.call("POST", "/packing/plan", tokens["shipper"],
                    json={"items": sample["items"], "truck_type": None, "cold_chain": sample["cold_chain"]})
    assert not plan["unplaced"]
    STATE.update(sample=sample, truck=plan["truck"]["name"], weight=plan["metrics"]["loaded_weight"])


def test_packing_plan_async_actual_celery_or_503(api, tokens):
    sample, truck = need("sample", "truck")
    if not STATE.get("redis_ok"):   # 브로커가 없으면 Celery 가 연결을 오래 재시도하므로 바로 건너뜀
        pytest.skip("작업 큐(Redis · Celery 워커) 없음 — 도커 스택(E2E_API_BASE)에서 점검")
    r = api.raw("POST", "/packing/plan-async", tokens["shipper"],
                json={"items": sample["items"], "truck_type": None, "cold_chain": sample["cold_chain"]})
    if r.status_code == 503:
        pytest.skip("작업 큐(Redis · Celery 워커) 없음 — 도커 스택(E2E_API_BASE)에서 점검")
    assert r.status_code == 202, r.text
    res = {"status": "PENDING"}
    for _ in range(60):
        res = api.call("GET", f"/packing/tasks/{r.json()['task_id']}", tokens["shipper"])
        if res["status"] in ("SUCCESS", "FAILURE"):
            break
        time.sleep(0.5)
    assert res["status"] == "SUCCESS" and res.get("ok") and res["result"]["truck"]["name"] == truck


def test_pricing_estimate_actual(api, tokens):
    sample, truck, weight = need("sample", "truck", "weight")
    est = api.call("POST", "/pricing/estimate", tokens["shipper"],
                   json={"truck_type": truck, "origin": ORIGIN, "destination": DEST,
                         "cold_chain": sample["cold_chain"], "weight_kg": weight})
    assert est["recommended"] > 0 and any(f["key"] == "weight" for f in est["factors"])
    api.call("POST", "/pricing/estimate", tokens["shipper"],
             json={"truck_type": truck, "origin": "제주", "destination": DEST}, expect=400)
    STATE["est"] = est


def test_create_order_actual_auto_matching(api, tokens):
    sample, truck, est = need("sample", "truck", "est")
    body = {"title": "e2e 테스트 화물", "truck_type": truck, "origin": ORIGIN, "destination": DEST,
            "price": est["recommended"], "cold_chain": sample["cold_chain"], "items": sample["items"]}
    api.call("POST", "/shipper/orders", tokens["shipper"], json={**body, "origin": "아무데나 창고"}, expect=400)
    order = api.call("POST", "/shipper/orders", tokens["shipper"], json=body, expect=201)
    assert order["matching"]["state"] in ("OFFERED", "FAILED", "SEARCHING")
    assert len(order["route"]) > 2 and order["fare_est"]["recommended"] == est["recommended"]


# ---------------------------------------------------------------- 기사: 제안 · 추천 · 수락
def test_driver_accepts_offer_or_recommendation(api, tokens):
    driver = tokens["driver"]
    profile = api.call("GET", "/driver/profile", driver)
    if profile["run"]:
        STATE["target"] = profile["run"]["id"]
        return
    recs = api.call("GET", "/driver/recommendations", driver)
    assert isinstance(recs, list)
    if recs:
        assert {"score", "pickup_km", "util"} <= recs[0]["rec"].keys() and "fare_est" in recs[0]
    offer = api.call("GET", "/driver/offer", driver)
    if offer:
        assert offer["matching"]["expires_in"] > 0
        STATE["target"] = api.call("POST", f"/driver/offer/{offer['id']}/accept", driver)["id"]
        return
    free = [r for r in recs if not r["rec"]["offered_to_other"]]
    assert free, "김철수 기사에게 수락 가능한 제안 · 추천 화물이 없음"
    STATE["target"] = api.call("POST", f"/driver/recommendations/{free[0]['id']}/accept", driver)["id"]


# ---------------------------------------------------------------- 운행 → 인수증 → 청구 → 결제
def test_run_start_and_gps_ping(api, tokens):
    (target,) = need("target")
    driver = tokens["driver"]
    run = api.call("GET", f"/orders/{target}", driver)
    if run["status"] == "ASSIGNED":
        run = api.call("POST", f"/driver/runs/{target}/start", driver)
    assert run["status"] == "IN_TRANSIT"
    lon, lat = run["route"][1]
    pong = api.call("POST", "/tracking/ping", driver, expect=202,
                    json={"order_id": target, "lat": lat, "lon": lon, "speed": 80})
    assert pong["accepted"]
    assert api.raw("POST", "/tracking/ping", json={"order_id": target, "lat": lat, "lon": lon}).status_code == 401


def test_complete_run_actual_requires_photo_then_delivers(api, tokens):
    (target,) = need("target")
    driver = tokens["driver"]
    api.call("POST", f"/driver/runs/{target}/complete", driver, expect=422, data={"receiver": "홍길동"})
    done = api.call("POST", f"/driver/runs/{target}/complete", driver, data={"receiver": "홍길동"},
                    files={"photo": ("pod.png", PNG, "image/png")})
    assert done["status"] == "DELIVERED" and done["pod"]["hash"].startswith("0x")
    assert len(done["pod"]["photo"]["sha256"]) == 64
    r = api.raw("GET", f"/orders/{target}/pod/photo", tokens["admin"])
    assert r.status_code == 200 and r.content == PNG
    STATE["delivered"] = target


def test_invoice_and_payment(api, tokens):
    (target,) = need("delivered")
    api.call("POST", f"/driver/completed/{target}/invoice", tokens["driver"], json={"extra_fee": 10_000, "toll_fee": 5_000})
    owner = api.call("GET", f"/orders/{target}", tokens["admin"])["shipper"]
    STATE["owner_is_demo"] = owner == "삼성물류"
    if STATE["owner_is_demo"]:
        paid = api.call("POST", f"/shipper/history/{target}/approve-invoice", tokens["shipper"])
        assert paid["status"] == "SETTLED"


def test_review_after_delivery(api, tokens):
    target, is_demo = need("delivered", "owner_is_demo")
    who = tokens["shipper"] if is_demo else tokens["driver"]
    review = api.call("POST", "/reviews", who, expect=201,
                      json={"order_id": target, "stars": 5, "tags": ["시간 엄수"] if is_demo else ["상하차 신속"]})
    assert review["stars"] == 5


# ---------------------------------------------------------------- 컴플라이언스 · 감사 로그
def test_compliance_check_actual(api):
    chk = api.call("GET", "/compliance/check", params={"biz_no": "123-45-67890", "plate": "부산 11가 2233"})
    assert not chk["business"]["ok"] and chk["plate"]["plate_type"] == "PRIVATE_WHITE"


def test_register_actual_fake_business_number_rejected(api):
    r = api.raw("POST", "/auth/register", data={"role": "shipper", "email": f"e2e{int(time.time())}@x.com",
                                                "password": "password1", "name": "e2e", "company": "e2e물류",
                                                "biz_no": "123-45-67890"})
    assert r.status_code == 400


def test_audit_chain_verifies(api, tokens):
    v = api.call("GET", "/admin/audit/verify", tokens["admin"])
    assert v["ok"], v.get("reason")


# ---------------------------------------------------------------- 관제 화면 API
@pytest.mark.parametrize("path, check", [
    ("/admin/summary", lambda r: r["vehicles"] > 0),
    ("/admin/fleet", lambda r: all(len(v["path"]) > 2 for v in r if v["path"])),
    ("/admin/matching", lambda r: "kpi" in r),
    ("/admin/matching/metrics", lambda r: r["ml_threshold"] == 0.75),
    ("/admin/reputation", lambda r: any(x["blacklisted"] for x in r)),
    ("/admin/tracking", lambda r: "runs" in r),
    ("/pricing/market", lambda r: len(r) >= 10 and all("ds_ratio" in x for x in r)),
])
def test_admin_views_smoke(api, tokens, path, check):
    assert check(api.call("GET", path, tokens["admin"]))


def test_ai_agent_actual_answers(api, tokens):
    ans = api.call("POST", "/admin/agent/ask", tokens["admin"], json={"text": "매칭 실패 보여줘"})
    assert ans["answer"] and ans["engine"]
