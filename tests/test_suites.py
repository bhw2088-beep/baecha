"""
pytest 진입점: 점검 스크립트(test_packing · checks_6/3_packing_rules · test_matching · checks_6/4_matching_ai · checks_6/5_ai_control · test_fsm · test_compliance · test_pricing ·
test_tracking · stress_test --quick · smoke_test)를 각각 실행해
'결과: N개 통과 · 0개 실패'(종료 코드 0)인지 확인한다.

점검 스크립트는 pytest 없이도 단독 실행할 수 있게 만들어져 있어서(python tests/basic/test_fsm.py),
pytest 는 이 파일만 모아(pytest.ini) 스크립트를 하위 프로세스로 돌린다.
smoke_test 는 실행 중인 백엔드가 필요 → 서버가 없으면 건너뛴다(skip).
"""

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BAECHA_DB": "0", "PYTHONIOENCODING": "utf-8", "NTS_API_KEY": "", "GEMINI_API_KEY": ""}
    return subprocess.run([sys.executable, os.path.join(HERE, script), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)


@pytest.mark.parametrize("script", ["basic/test_packing.py", "checks_6/3_packing_rules.py", "basic/test_matching.py",
                                    "checks_6/4_matching_ai.py", "checks_6/5_ai_control.py", "basic/test_fsm.py",
                                    "basic/test_compliance.py", "basic/test_pricing.py", "basic/test_tracking.py",
                                    "stress/stress_test.py --quick"])
def test_offline_suite(script):
    """서버 없이 도는 점검 (3D 적재 · 매칭/거리/운임 · AI 관제 권한/개인정보 · FSM/감사 로그/모의 PG · 컴플라이언스 · 수급/안전율 ·
    추적/해시 체인/리뷰 · 마이크로 배치 스트레스)."""
    r = _run(*script.split())
    tail = "\n".join(r.stdout.strip().splitlines()[-15:])
    assert r.returncode == 0, f"{script} 실패\n{tail}\n{r.stderr[-2000:]}"
    assert "0개 실패" in r.stdout


def test_smoke_api(server_up, api_base):
    """실행 중인 백엔드 API 전체 흐름 (등록 → 매칭 → 수락 → 운행 → 청구 → 결제, Celery 포함)."""
    if not server_up:
        pytest.skip(f"백엔드가 실행 중이 아님 ({api_base})")
    r = _run("server/smoke_test.py", api_base)
    tail = "\n".join(r.stdout.strip().splitlines()[-15:])
    assert r.returncode == 0, f"smoke_test 실패\n{tail}\n{r.stderr[-2000:]}"
