"""
매칭 2단계 피처: 기사-권역 선호도 cf_score (0~1)  ※ ALS 협업 필터링의 Cold-Start 대체 휴리스틱

ALS(Alternating Least Squares)는 '기사 × 권역' 수락 행렬이 충분히 쌓여야 학습할 수 있다.
실제 운행 기록이 없는 지금은 아래 휴리스틱으로 같은 의미의 점수를 만든다.
기록이 쌓이면 이 함수만 ALS 예측값으로 바꾸면 된다 (반환 범위 0~1 유지).

  기본 0.5
  + 0.20  상차 권역 = 기사의 주 대기 권역 (익숙한 지역)
  + 0.15  하차 권역 = 기사의 주 대기 권역 (회차 · 귀가 운행)
  + 0.08 × (그 상차 권역에서 수락 · 운행한 횟수, 최대 3회)
  − 0.12 × (그 상차 권역 화물을 거절 · 무응답한 횟수, 최대 3회)
"""

from __future__ import annotations

from ..pricing_engine import region_of


def cf_score(driver_home: str, origin: str, destination: str, accepted_regions: list[str],
             declined_regions: list[str]) -> float:
    home = region_of(driver_home)
    o_reg, d_reg = region_of(origin), region_of(destination)
    s = 0.5
    if o_reg == home:
        s += 0.20
    if d_reg == home:
        s += 0.15
    s += 0.08 * min(3, accepted_regions.count(o_reg))
    s -= 0.12 * min(3, declined_regions.count(o_reg))
    return round(max(0.0, min(1.0, s)), 3)
