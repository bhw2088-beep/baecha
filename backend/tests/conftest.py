"""
pytest 공통 설정 (모든 테스트 공용)

테스트 구성 (backend 폴더에서  python -m pytest)
  tests/unit_tests/    단위 테스트 — 서버 · DB · Redis 없이 함수 · 클래스 하나씩 검증 (기본 실행)
  tests/e2e_tests/     API 전체 흐름 — 기본은 프로세스 안 FastAPI TestClient, E2E_API_BASE 를 주면 실행 중인 서버로
  tests/stress_tests/  마이크로 배치 · 대량 스트레스 (빠른 버전만 pytest 로, 8,000건 이상은 번호 붙은 실행 파일로)

단위 테스트 이름 규칙 (TDD)
  test_ctor_*          생성자 · 기본값 · 데이터 구조가 의도대로 만들어지는가
  test_<대상>_smoke    정상 입력을 넣으면 오류 없이 기대한 형태(키 · 타입 · 범위)의 결과가 나오는가
  test_<대상>_actual_* 실제 규칙 · 경계값 · 실패 케이스가 맞는가 (코드를 잘못 고치면 여기서 깨진다)

환경
  - backend 폴더 · 저장소 최상위를 import 경로에 추가 → src · simulator 패키지를 바로 import
  - BAECHA_DB=0 → 메모리 저장소만 (실제 PostgreSQL 데이터를 건드리지 않음)
  - 외부 API(국세청 · Gemini) 키는 비워서 항상 모의 판정 · 규칙 기반으로
"""

import os
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))   # backend 폴더 → src 패키지
REPO = os.path.dirname(BACKEND)                                             # 저장소 최상위 → simulator 패키지
for _p in (BACKEND, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ["BAECHA_DB"] = "0"
os.environ["NTS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
# 로컬에 Redis 가 없어도 바로 '없음'으로 판정되게 (닫힌 포트) — 실행 중인 서버 대상 e2e 는 영향 없음
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6390/0")


@pytest.fixture(scope="session")
def _seed_template():
    """시드 데이터로 한 번만 만드는 원본 저장소 (만드는 데 약 1.5초)."""
    from src.orm.database import Store
    return Store()


@pytest.fixture
def store(_seed_template):
    """테스트마다 원본을 통째로 복사한 새 저장소 (약 0.04초 · 테스트끼리 서로 영향 없음).
    RLock 은 복사할 수 없어 새 락으로 바꿔 넣는다."""
    import copy
    import threading
    return copy.deepcopy(_seed_template, {id(_seed_template.lock): threading.RLock()})


@pytest.fixture
def seed_store(store):
    """읽기만 하는 테스트용 이름 (store 와 같음 · 값을 바꿔도 다른 테스트에 영향 없음)."""
    return store
