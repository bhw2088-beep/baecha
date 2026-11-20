"""
③ 상부 허용하중 안전 여유 비교 — 여유 0% vs 10% · 시드 7 · 각 8,008건 (순서대로)      약 15분

VS Code: 이 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 바로 실행되고, 끝나면 전후 비교표가 나온다.
  - 화주 신고 무게가 실제와 −5 ~ +10% 다를 때 '실제 무게'로 다시 재서 물리 한계를 넘는 비율(현실 위험률)과
    그 대가(적재율 · 매칭률 · 거절률 · 공차 단축률 · 배차 지연)를 나란히 비교
  - KPI 기준 결과(results/benchmark_large.json)는 건드리지 않음
  - 결과: results/margin_compare/margin_00.json · margin_10.json · summary.md
  - 빠르게 확인만: python backend/tests/stress_tests/3_margin_compare.py --quick   (하루 1,001건 · 1~2분)
  - 실행이 성공하면 비교표가 이 파일 맨 아래 ''' ''' 블록에 자동으로 저장된다 (문자열이라 실행되지 않음)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kpi  # noqa: E402
import _margin  # noqa: E402

if __name__ == "__main__":
    rc = _margin.main(sys.argv[1:])
    if rc == 0 and _margin.LAST_TEXT:
        _kpi.save_result_block(__file__, _margin.LAST_TEXT)
    sys.exit(rc)


# ===== 마지막 실행 결과 (▷ 실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) =====
'''
저장 2026-11-18 09:14

아직 실행 결과 없음 — ▷ 로 실행하면 여유 0% vs 10% 비교표가 여기에 저장됩니다.
'''
