"""
스트레스 테스트 (빠른 버전) — pytest 로 돌리는 것은 이 파일뿐이다.

  stress_test.py --quick  : 가상 기사 18 · 주문 30 · 마이크로 배치 · 위반 화물 주입 · KPI 판정
  packing_stress.py --cases 60 : 무작위 화물 60건 × 상부 허용하중 여유 · 신고 무게 오차

8,000건 이상 대량 측정은 번호 붙은 실행 파일(1_kpi_8k.py ~ 5_packing_stress.py)로 따로 돌린다 (README.md).
두 스크립트는 단독 실행형이라 하위 프로세스로 돌리고 '0개 실패'(종료 코드 0)를 확인한다.
"""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.stress

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(module: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BAECHA_DB": "0", "PYTHONIOENCODING": "utf-8", "NTS_API_KEY": "", "GEMINI_API_KEY": ""}
    return subprocess.run([sys.executable, "-m", module, *args], cwd=BACKEND, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=900)


def _tail(r: subprocess.CompletedProcess) -> str:
    return "\n".join(r.stdout.strip().splitlines()[-20:]) + "\n" + r.stderr[-2000:]


def test_stress_quick_micro_batch():
    r = _run("tests.stress_tests.stress_test", "--quick")
    assert r.returncode == 0 and "0개 실패" in r.stdout, _tail(r)


def test_stress_quick_packing(tmp_path):
    out = tmp_path / "packing.json"
    r = _run("tests.stress_tests.packing_stress", "--cases", "60", "--margins", "0", "0.1", "--errors=-5:10",
             "--workers", "2", "--out", str(out))
    assert r.returncode == 0, _tail(r)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["cases"] == 60 and data["clean_cases"] + data["bad_cases"] == 60
    assert data["by_margin"], "여유값별 집계가 비어 있음"
