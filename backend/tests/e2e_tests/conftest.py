"""
e2e 공통: API 클라이언트 fixture.

  - 기본: 프로세스 안에서 FastAPI 앱을 띄워(TestClient) 메모리 저장소로 실행 → 서버 · DB · Redis 없이 돈다.
  - E2E_API_BASE=http://localhost:8000/api/v1 을 주면 실행 중인 서버(도커 등)에 HTTP 로 요청한다 (CI 통합 점검).
"""

import os

import pytest

PW = "baecha1234"
ACCOUNTS = {"shipper": "logistics@samsung.com", "driver": "driver.kim@baecha.co.kr",
            "admin": "admin.control@baecha.co.kr"}


class Api:
    """요청 한 번 = 상태 코드 확인 + JSON 반환. live 이면 requests, 아니면 TestClient."""

    def __init__(self, client, base: str, live: bool):
        self.client, self.base, self.live = client, base, live

    def raw(self, method: str, path: str, token: str | None = None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if self.live:
            kw.setdefault("timeout", 30)
        return self.client.request(method, self.base + path, headers=headers, **kw)

    def call(self, method: str, path: str, token: str | None = None, expect: int = 200, **kw):
        r = self.raw(method, path, token, **kw)
        assert r.status_code == expect, f"{method} {path} → {r.status_code} (기대 {expect}) {r.text[:300]}"
        return r.json()

    def login(self, role: str) -> str:
        return self.call("POST", "/auth/login", json={"email": ACCOUNTS[role], "password": PW})["token"]


@pytest.fixture(scope="module")
def api():
    base = os.getenv("E2E_API_BASE")
    if base:
        import requests
        s = requests.Session()
        try:
            s.get(base + "/health", timeout=5).raise_for_status()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"E2E_API_BASE 서버에 연결할 수 없음 ({base}): {e}")
        yield Api(s, base.rstrip("/"), live=True)
        s.close()
        return
    from fastapi.testclient import TestClient

    from src.main import app
    with TestClient(app) as client:
        yield Api(client, "/api/v1", live=False)


@pytest.fixture(scope="module")
def tokens(api):
    return {role: api.login(role) for role in ACCOUNTS}
