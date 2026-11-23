"""
동시성 테스트 — 같은 요청이 동시에 여러 번 들어와도 한 번만 처리되는지 (중복 배차 · 중복 결제)        약 2~3분

VS Code: 이 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 바로 실행된다.
  - 테스트 전용 스택(PostgreSQL · Redis · 백엔드, 프로젝트 이름 baecha-conc)을 따로 띄우고 끝나면 지운다
    → 데모 스택(8000 · 8501) · 부하 테스트 스택(baecha-load)과 데이터 · 포트가 겹치지 않음
  - 백엔드는 지금 폴더의 코드를 읽기 전용으로 넣어서 실행 (이미지 다시 빌드 불필요)
  - 도커 이미지(baecha-app:dev) 필요 → Docker Desktop 이 켜져 있어야 함
터미널: 수정본 폴더에서  python tests/checks_6/1_concurrency.py

점검 내용 (요청은 스레드 여러 개로 '같은 순간'에 보냄)
  0. 같은 기사 가입 요청을 5번 동시에                          → 계정 1개 (이후 테스트 기사 12명 가입 · 승인)
  1. 같은 기사가 같은 배차 제안을 10번 동시에 수락            → 1번만 성공 · 배차 1건
  2. 제안받은 기사 수락 + 다른 기사들이 AI 추천에서 동시에 선택  → 1명만 배차 (제안받은 기사)
  3. 기사 수락과 관제 수동 배정이 동시에                       → 둘 중 하나만 성공 · 기사 1명
  4. 화주 청구서 승인(모의 결제)을 10번 동시에                  → 결제 1건 · 감사 로그 1줄
  5. 관제 결제 취소(환불)를 10번 동시에                         → 취소 1건
  6. 관제 일괄 송금을 5번 동시에                                → 화물마다 송금 1번
  7. 끝난 뒤 전체 점검: 기사 한 명에게 진행 중 운행이 2건 이상인 경우 없음
  8. 같은 화물 등록 요청(같은 Idempotency-Key)을 5번 동시에        → 화물 1건 · 같은 키 다른 내용은 409

왜 '한 번만' 되는가: 백엔드는 저장소 변경을 이벤트 루프 하나에서 순서대로 처리하고(요청 처리 함수 안에 대기 없음),
각 처리 함수가 상태를 먼저 확인한다(이미 배차됨 · 이미 결제됨 → 409). 이 테스트는 그 전제가 실제로 지켜지는지 확인한다.
pytest · CI 의 자동 실행 목록(tests/test_suites.py)에는 넣지 않았다 (도커 스택을 직접 띄우기 때문).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # 수정본
IMAGE = "baecha-app:dev"
PROJECT = "baecha-conc"
NET = f"{PROJECT}_default"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "scripts/docker-compose.loadtest.yml",
           "-f", "tests/checks_6/_concurrency_compose.yml"]
PW = "baecha1234"
SHIPPER, ADMIN = "logistics@samsung.com", "admin.control@baecha.co.kr"


# ====================================================================== 바깥(이 PC): 테스트 스택 준비
def _run(cmd, quiet=True, timeout=600):
    return subprocess.run(cmd, cwd=ROOT, capture_output=quiet, text=True, encoding="utf-8", timeout=timeout,
                          env={**os.environ, "MSYS_NO_PATHCONV": "1"})


def host_main() -> int:
    try:
        if _run(["docker", "image", "inspect", IMAGE]).returncode != 0:
            print(f"도커 이미지 {IMAGE} 가 없습니다. 수정본 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
            return 1
    except FileNotFoundError:
        print("docker 명령을 찾을 수 없습니다. Docker Desktop 을 켠 뒤 다시 실행하세요.")
        return 1
    print("테스트 전용 스택(baecha-conc) 준비 중... (데모 · 부하 테스트 스택과 분리, 30초~1분)")
    _run([*COMPOSE, "down", "-v", "--remove-orphans"])
    try:
        r = _run([*COMPOSE, "up", "-d"])
        if r.returncode != 0:
            print("스택 시작 실패:\n" + (r.stderr or r.stdout)[-1500:])
            return 1
        r = subprocess.run(
            ["docker", "run", "--rm", "--network", NET, "-e", "PYTHONPATH=/src", "-e", "PYTHONIOENCODING=utf-8",
             "-v", f"{ROOT.as_posix()}:/src", "-w", "/src", IMAGE,
             "python", "tests/checks_6/1_concurrency.py", "--inside", "http://backend:8000/api/v1"],
            env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        if r.returncode not in (0, 1):
            print(_run([*COMPOSE, "logs", "--tail", "60", "backend"]).stdout)
        return r.returncode
    finally:
        _run([*COMPOSE, "down", "-v", "--remove-orphans"])
        print("테스트 스택 정리 완료")


# ====================================================================== 안(도커 네트워크): 실제 점검
API = ""


def call(method: str, path: str, token: str | None = None, **kw):
    import requests
    h = {**kw.pop("headers", {}), **({"Authorization": f"Bearer {token}"} if token else {})}
    r = requests.request(method, API + path, headers=h, timeout=60, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:200]
    return r.status_code, body


def ok(method, path, token=None, **kw):
    code, body = call(method, path, token, **kw)
    if code not in (200, 201, 202):
        raise AssertionError(f"{method} {path} → {code} {body}")
    return body


def burst(fns: list) -> list[tuple[int, object]]:
    """함수 여러 개를 스레드로 '같은 순간'에 실행 (시작 신호를 맞춰서)."""
    import threading
    gate = threading.Barrier(len(fns))

    def run(fn):
        gate.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        return list(ex.map(run, fns))


def wait_api(timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if call("GET", "/health")[0] == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


DOC_FILES = {"license_photo": ("license.jpg", b"\xff\xd8\xff conc license", "image/jpeg"),
             "cargo_license_photo": ("cargo_license.jpg", b"\xff\xd8\xff conc cargo", "image/jpeg"),
             "vehicle_photo": ("vehicle.jpg", b"\xff\xd8\xff conc vehicle", "image/jpeg")}
N_TEST_DRIVERS = 12
HUB = "경기 평택항 물류단지"   # 테스트 화물 상차지 · 테스트 기사 위치 (가까운 기사에게 제안이 가도록)


def biz_no(first9: int) -> str:
    """앞 9자리 → Modulus-10 검증 숫자를 붙인 사업자등록번호 (서버와 같은 계산)."""
    w = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    d = [int(c) for c in f"{first9:09d}"]
    s = sum(x * k for x, k in zip(d, w)) + (d[8] * 5) // 10
    return f"{first9:09d}{(10 - s % 10) % 10}"


def driver_form(i: int) -> dict:
    n = 7_300 + i
    return {"role": "driver", "email": f"conc-d{i}@conc.test", "password": PW, "name": f"동시성기사{i:02d}",
            "phone": f"010-73{n // 10_000:02d}-{n % 10_000:04d}", "biz_no": biz_no(731_000_000 + i * 7),
            "plate": f"경기 9{i % 10}바 {7300 + i:04d}", "truck_type": "11톤 탑차", "base_location": HUB}


class Ctx:
    def __init__(self):
        import re
        self.shipper = ok("POST", "/auth/login", json={"email": SHIPPER, "password": PW})["token"]
        self.admin = ok("POST", "/auth/login", json={"email": ADMIN, "password": PW})["token"]
        src = (ROOT / "backend" / "core" / "seed.py").read_text(encoding="utf-8")
        self.drivers: dict[str, str] = {}
        for email in re.findall(r'"email":\s*"([^"]+)",\s*"role":\s*"driver"', src):
            code, b = call("POST", "/auth/login", json={"email": email, "password": PW})
            if code == 200:
                self.drivers[b["user"]["org_id"]] = b["token"]
        self.sample = next(iter(ok("GET", "/packing/meta", self.shipper)["samples"].values()))

    def add_test_drivers(self, start: int = 1, count: int = N_TEST_DRIVERS) -> int:
        """테스트 기사 가입(서류 3종) → 관제 승인. 승인된 기사만 매칭 후보가 된다."""
        added = 0
        for i in range(start, start + count):
            code, b = call("POST", "/auth/register", data=driver_form(i), files=DOC_FILES)
            if code != 200:
                continue
            did = b["user"]["org_id"]
            if call("POST", f"/admin/compliance/drivers/{did}", self.admin, json={"approve": True})[0] == 200:
                self.drivers[did] = b["token"]
                added += 1
        return added

    def new_order(self, title: str) -> dict:
        s = self.sample
        origin, dest = HUB, "충남 아산시 탕정면"
        est = ok("POST", "/pricing/estimate", self.shipper, json={"truck_type": "11톤 탑차", "origin": origin,
                                                                  "destination": dest, "cold_chain": s["cold_chain"]})
        body = {"title": title, "truck_type": "11톤 탑차", "origin": origin, "destination": dest,
                "price": int(est["recommended"] * 1.3), "cold_chain": s["cold_chain"], "items": s["items"]}
        return ok("POST", "/shipper/orders", self.shipper, json=body)

    def order(self, oid: str) -> dict:
        return ok("GET", f"/orders/{oid}", self.admin)

    def offered_order(self, title: str, timeout=60) -> tuple[dict, str] | None:
        """새 화물을 등록하고, 로그인한 기사 중 누군가에게 배차 제안이 갈 때까지 기다린다 → (화물, 제안받은 기사)."""
        for _attempt in range(2):
            o = self.new_order(title)
            end = time.time() + timeout
            while time.time() < end:
                v = self.order(o["id"])
                m = v.get("matching") or {}
                if m.get("state") == "OFFERED" and m["driver"]["id"] in self.drivers:
                    return v, m["driver"]["id"]
                if v["status"] != "MATCHING":
                    break
                time.sleep(1)
        return None

    def active_runs(self) -> dict[str, list[str]]:
        runs: dict[str, list[str]] = {}
        for did, tok in self.drivers.items():
            p = ok("GET", "/driver/profile", tok)
            if p.get("run"):
                runs.setdefault(did, []).append(p["run"]["id"])
        return runs


def free_drivers(ctx: Ctx):
    """다음 시나리오를 위해: 테스트가 배차한 운행을 정리하지는 않고, 진행 중 운행이 없는 기사만 쓴다."""
    busy = set(ctx.active_runs())
    return [d for d in ctx.drivers if d not in busy]


def inside_main(api: str) -> int:
    global API
    API = api
    sys.path.insert(0, str(ROOT / "tests"))
    from _check import check, finish, section

    if not wait_api():
        print("백엔드가 응답하지 않습니다.")
        return 2
    ctx = Ctx()

    # ---------------------------------------------------------------- 0
    section("0. 같은 기사 가입 요청을 5번 동시에 (이메일 · 연락처 · 사업자번호 · 차량번호 같음)")
    res = burst([lambda: call("POST", "/auth/register", data=driver_form(0), files=DOC_FILES)] * 5)
    codes = [c for c, _ in res]
    check("5번 중 1번만 가입 · 나머지는 409", codes.count(200) == 1 and codes.count(409) == 4, f"응답 {sorted(codes)}")
    n = ctx.add_test_drivers()
    print(f"로그인: 화주 1 · 관제 1 · 기사 {len(ctx.drivers)}명 (시드 {len(ctx.drivers) - n} + 테스트 가입 · 승인 {n})")

    # ---------------------------------------------------------------- 1
    section("1. 같은 기사가 같은 배차 제안을 10번 동시에 수락")
    got = ctx.offered_order("동시성-1 같은 제안 중복 수락")
    check("배차 제안 준비", got is not None)
    if got:
        o, did = got
        res = burst([lambda: call("POST", f"/driver/offer/{o['id']}/accept", ctx.drivers[did])] * 10)
        codes = [c for c, _ in res]
        after = ctx.order(o["id"])
        check("10번 중 1번만 성공 · 나머지는 409", codes.count(200) == 1 and codes.count(409) == 9, f"응답 {sorted(codes)}")
        check("화물은 그 기사 1명에게 배차", after["status"] != "MATCHING" and (after["driver"] or {}).get("id") == did,
              f"{after['status']} · {(after['driver'] or {}).get('name')}")

    # ---------------------------------------------------------------- 2
    section("2. 제안받은 기사 수락 + 다른 기사들이 AI 추천에서 같은 화물을 동시에 선택")
    got = ctx.offered_order("동시성-2 수락 vs 추천 선택")
    check("배차 제안 준비", got is not None)
    if got:
        o, did = got
        others = [d for d in free_drivers(ctx) if d != did][:9]
        fns = [lambda: call("POST", f"/driver/offer/{o['id']}/accept", ctx.drivers[did])]
        fns += [(lambda d=d: call("POST", f"/driver/recommendations/{o['id']}/accept", ctx.drivers[d])) for d in others]
        res = burst(fns)
        wins = [i for i, (c, _) in enumerate(res) if c == 200]
        after = ctx.order(o["id"])
        check(f"{len(fns)}명 중 1명만 성공", len(wins) == 1, f"성공 {len(wins)} · 응답 {sorted(c for c, _ in res)}")
        check("배차된 기사 = 제안받은 기사 (다른 기사가 가로채지 않음)", (after["driver"] or {}).get("id") == did)

    # ---------------------------------------------------------------- 3
    section("3. 기사 수락과 관제 수동 배정이 동시에")
    got = ctx.offered_order("동시성-3 수락 vs 수동 배정")
    check("배차 제안 준비", got is not None)
    if got:
        o, did = got
        cands = ok("GET", f"/admin/failed-matches/{o['id']}/candidates", ctx.admin)
        rows = cands if isinstance(cands, list) else cands.get("candidates", [])
        other = next((c["id"] for c in rows if c.get("id") != did), None)
        fns = [lambda: call("POST", f"/driver/offer/{o['id']}/accept", ctx.drivers[did])]
        if other:
            fns.append(lambda: call("POST", f"/admin/failed-matches/{o['id']}/assign", ctx.admin,
                                    json={"driver_id": other}))
        res = burst(fns * 3)        # 같은 조합을 3번씩 겹쳐서
        after = ctx.order(o["id"])
        n_ok = sum(1 for c, _ in res if c == 200)
        check("성공은 1번뿐", n_ok == 1, f"응답 {sorted(c for c, _ in res)} (수동 배정 후보 {'있음' if other else '없음'})")
        check("화물에 배차된 기사 1명", after["status"] != "MATCHING" and after["driver"] is not None,
              (after["driver"] or {}).get("name", "-"))

    # ---------------------------------------------------------------- 4 · 5 · 6
    section("4. 화주 청구서 승인(모의 결제)을 10번 동시에")
    hist = ok("GET", "/shipper/history", ctx.shipper)
    items = hist["items"] if isinstance(hist, dict) else hist
    req = [i for i in items if i["billing"] == "REQUESTED"]
    check("승인 대기 청구서 준비", len(req) >= 1, f"{len(req)}건")
    if req:
        oid = req[0]["id"]
        res = burst([lambda: call("POST", f"/shipper/history/{oid}/approve-invoice", ctx.shipper,
                                  json={"method": "corp_card"})] * 10)
        codes = [c for c, _ in res]
        after = ctx.order(oid)
        audit = ok("GET", "/admin/audit", ctx.admin, params={"order_id": oid, "financial": "true"})
        rows = audit if isinstance(audit, list) else audit.get("items", audit.get("rows", []))
        pays = [e for e in rows if e.get("event") == "pay"]
        check("10번 중 1번만 성공", codes.count(200) == 1, f"응답 {sorted(codes)}")
        check("결제 1건 · 청구 상태 PAID", after["billing"] == "PAID" and len(after.get("payments") or [after["payment"]]) == 1,
              after["billing"])
        check("감사 로그 '결제 승인' 1줄", len(pays) == 1, f"{len(pays)}줄")

        section("5. 관제 결제 취소(환불)를 10번 동시에")
        res = burst([lambda: call("POST", f"/admin/payments/{oid}/refund", ctx.admin,
                                  json={"reason": "동시성 테스트"})] * 10)
        codes = [c for c, _ in res]
        after = ctx.order(oid)
        check("10번 중 1번만 성공 · 나머지는 409", codes.count(200) == 1 and codes.count(409) == 9, f"응답 {sorted(codes)}")
        check("청구 상태 REFUNDED", after["billing"] == "REFUNDED", after["billing"])

    section("6. 관제 일괄 송금을 5번 동시에")
    if len(req) >= 2:   # 송금할 결제 건을 하나 더 만든다
        ok("POST", f"/shipper/history/{req[1]['id']}/approve-invoice", ctx.shipper, json={"method": "transfer"})
    before = ok("GET", "/admin/payouts", ctx.admin)
    pending_orders = {oid for r in before["pending"] for oid in r["orders"]}
    res = burst([lambda: call("POST", "/admin/payouts/run", ctx.admin, json={})] * 5)
    after = ok("GET", "/admin/payouts", ctx.admin)
    paid_out = [oid for p in after["history"] for oid in p["orders"] if oid in pending_orders]
    check("송금 대기였던 화물이 모두 송금됨", set(paid_out) == pending_orders and not after["pending"],
          f"대기 {len(pending_orders)}건 → 송금 {len(set(paid_out))}건")
    check("화물마다 송금 1번 (중복 송금 없음)", len(paid_out) == len(set(paid_out)),
          f"송금 기록 {len(paid_out)}줄 · 화물 {len(set(paid_out))}건 · 응답 {sorted(c for c, _ in res)}")

    # ---------------------------------------------------------------- 7
    section("7. 전체 점검")
    runs = ctx.active_runs()
    dup = {d: r for d, r in runs.items() if len(r) > 1}
    check("기사 한 명에게 진행 중 운행 2건 이상 없음", not dup, f"진행 중 운행 기사 {len(runs)}명")

    # ---------------------------------------------------------------- 8
    section("8. 같은 화물 등록 요청을 동시에 5번 (Idempotency-Key · 버튼 두 번 · 응답 지연 후 재시도)")
    import uuid
    s = ctx.sample
    body = {"title": "동시성-8 중복 등록", "truck_type": "11톤 탑차", "origin": HUB,
            "destination": "충남 아산시 탕정면", "price": 900000, "cold_chain": s["cold_chain"], "items": s["items"]}
    count = lambda: len(ok("GET", "/shipper/orders", ctx.shipper))  # noqa: E731
    key = uuid.uuid4().hex
    n0 = count()
    res = burst([lambda: call("POST", "/shipper/orders", ctx.shipper, json=body,
                              headers={"Idempotency-Key": key})] * 5)
    ids = {b["id"] for c, b in res if c == 201}
    check("같은 키 5번 → 화물 1건만 생성", count() - n0 == 1, f"{count() - n0}건 · 응답 {sorted(c for c, _ in res)}")
    check("5번 모두 같은 화물을 돌려줌 (재시도해도 같은 결과)", len(ids) == 1 and all(c == 201 for c, _ in res),
          ", ".join(ids))
    c, b = call("POST", "/shipper/orders", ctx.shipper, json={**body, "price": 950000},
                headers={"Idempotency-Key": key})
    check("같은 키 · 다른 내용 → 409 (다른 화물을 같은 키로 보낸 실수 차단)", c == 409, f"{c} {str(b)[:60]}")
    n1 = count()
    res = burst([lambda: call("POST", "/shipper/orders", ctx.shipper, json=body,
                              headers={"Idempotency-Key": uuid.uuid4().hex})] * 2)
    check("다른 키 2번 → 2건 (서로 다른 등록은 그대로)", count() - n1 == 2, f"{count() - n1}건")
    n2 = count()
    burst([lambda: call("POST", "/shipper/orders", ctx.shipper, json=body)] * 2)
    print(f"  [참고] 키 없이 2번 → {count() - n2}건 (키를 보내지 않는 예전 클라이언트 · 시뮬레이터는 기존 동작 유지)")

    finish()
    return 0


if __name__ == "__main__":
    if "--inside" in sys.argv:
        sys.exit(inside_main(sys.argv[sys.argv.index("--inside") + 1]))
    sys.exit(host_main())
