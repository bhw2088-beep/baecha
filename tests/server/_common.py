"""폴더 공용 연결: 상위 tests/ 의 _check(PASS/FAIL 집계 · 라이브러리 없으면 도커로 자동 실행)와
metrics/eval_matching_lgbm 를 쓸 수 있게 경로를 추가하고 그대로 다시 내보낸다.
이 폴더의 테스트는 `from _common import check, finish, section` 로 쓴다. (basic · checks_6 · stress · server 에 같은 파일)"""

import os
import sys

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 수정본/tests
for _p in (TESTS, os.path.join(TESTS, "metrics")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _check import ROOT, check, finish, section  # noqa: E402

__all__ = ["ROOT", "TESTS", "check", "finish", "section"]
