"""
pytest 공통 설정 (fixture)

- backend 폴더 · 저장소 최상위를 import 경로에 추가 → src · simulator 패키지를 바로 import
- BAECHA_DB=0 → 테스트는 메모리 저장소만 쓴다 (실제 PostgreSQL 데이터를 건드리지 않음)
- api_base fixture: smoke_test 가 쓸 API 주소 (환경변수 API_BASE, 기본 http://127.0.0.1:8000/api/v1)

실행: backend 폴더에서  python -m pytest -q
"""

import os
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))   # backend 폴더 → src 패키지
REPO = os.path.dirname(BACKEND)                                             # 저장소 최상위 → simulator 패키지
for _p in (BACKEND, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("BAECHA_DB", "0")
os.environ["NTS_API_KEY"] = ""   # 테스트는 외부 API(국세청 · Gemini)를 부르지 않는다
os.environ["GEMINI_API_KEY"] = ""


@pytest.fixture(scope="session")
def api_base() -> str:
    return os.getenv("API_BASE", "http://127.0.0.1:8000/api/v1")


@pytest.fixture(scope="session")
def server_up(api_base) -> bool:
    try:
        import requests
        return requests.get(f"{api_base}/health", timeout=3).ok
    except Exception:  # noqa: BLE001
        return False
