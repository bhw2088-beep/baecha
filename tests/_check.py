"""테스트 스크립트 공용: 간단한 PASS/FAIL 집계 (pytest 없이 실행)."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # 수정본 폴더 → backend 패키지를 import 할 수 있게

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
