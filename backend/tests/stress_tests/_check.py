"""스트레스 스크립트 공용: 간단한 PASS/FAIL 집계 (pytest 없이 단독 실행하는 stress_test.py · packing_stress.py 용).

backend 폴더에서 모듈로 실행한다:  python -m tests.stress_tests.stress_test --quick
"""

import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))   # backend 폴더 → src 패키지
REPO = os.path.dirname(BACKEND)                                             # 저장소 최상위 → simulator 패키지
for _p in (BACKEND, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("BAECHA_DB", "0")  # 테스트는 메모리 저장소만 사용 (실제 DB 데이터를 건드리지 않음)
# 테스트는 항상 모의 판정 · 규칙 기반으로 (.env 에 실제 국세청 · Gemini 키가 있어도 외부 API 를 부르지 않음)
os.environ["NTS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""

_result = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = ""):
    _result["pass" if ok else "fail"] += 1
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def section(title: str):
    print(f"\n■ {title}")


def finish():
    p, f = _result["pass"], _result["fail"]
    print(f"\n결과: {p}개 통과 · {f}개 실패")
    sys.exit(1 if f else 0)
