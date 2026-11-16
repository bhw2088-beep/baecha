"""
Phase 1: 날씨 · 권역별 수급(D/S) 모의 모델 (순수 함수 · 외부 API 없음)

[날씨]  mock_get_weather_condition(권역, 시각)
  기상청 API 대신 확률 분포로 날씨를 뽑는다 (같은 권역 · 같은 시간대에는 같은 결과).
    맑음 72% (할증 없음) · 비 22% (+15%) · 폭설 6% (+30%)   ※ 12~2월은 폭설 확률을 높인다
  할증은 '기본 운임 × 배율'로 운임 계산에 더한다.

[수급 지수]  D/S = 수요(화물 콜) ÷ 공급(빈 차량)
  수요 = 실제 매칭 대기 화물 + 시간대별 포아송 분포로 만든 가상 콜
         (λ = 권역 기본 콜 수 × 시간대 배율, 러시아워 08~09시 · 18~19시에 폭증)
  공급 = 실제 빈 기사 + 권역 기본 공차 수 (러시아워에는 정체로 20% 감소)
  D/S > 1.5 이면 서지 프라이싱 자동 발동: +10% 부터 비율에 따라 최대 +30%.
"""

from __future__ import annotations

import math
import random
from datetime import datetime

# ------------------------------------------------------------------ 시간대
RUSH_HOURS = (8, 18)  # 08:00~08:59 · 18:00~18:59  (오전 8~9시 · 오후 6~7시)
HOUR_PROFILE = {h: (3.0 if h in RUSH_HOURS else 1.2 if 9 <= h <= 17 else 0.9 if h in (6, 7, 19, 20, 21) else 0.3)
                for h in range(24)}


def is_rush_hour(at: datetime | None = None) -> bool:
    return (at or datetime.now()).hour in RUSH_HOURS


def region_of(address: str) -> str:
    """주소 → 권역(시 · 도). '인천항 물류센터' → 인천, '부산항' → 부산, '경기 평택시' → 경기."""
    word = (address or "").split()[0] if address else ""
    return word[:2] if word else "기타"


def _rng(*key) -> random.Random:
    return random.Random("|".join(map(str, key)))


# ------------------------------------------------------------------ 날씨
WEATHER = {
    "CLEAR": {"label": "맑음", "multiplier": 0.00},
    "RAIN": {"label": "비", "multiplier": 0.15},
    "SNOW": {"label": "폭설", "multiplier": 0.30},
}


def weather_distribution(month: int) -> dict[str, float]:
    if month in (12, 1, 2):
        return {"CLEAR": 0.66, "RAIN": 0.14, "SNOW": 0.20}
    return {"CLEAR": 0.72, "RAIN": 0.22, "SNOW": 0.06}


def mock_get_weather_condition(region: str, at: datetime | None = None) -> dict:
    """기상청 모의 API: 권역 · 날짜 · 시간대별로 확률 분포에서 날씨를 뽑는다 (같은 시간대엔 같은 값)."""
    at = at or datetime.now()
    dist = weather_distribution(at.month)
    r = _rng("weather", region, at.strftime("%Y%m%d%H")).random()
    acc, code = 0.0, "CLEAR"
    for code, p in dist.items():
        acc += p
        if r < acc:
            break
    w = WEATHER[code]
    return {"region": region, "code": code, "label": w["label"], "multiplier": w["multiplier"],
            "probability": dist[code], "distribution": dist, "source": "모의 기상 (확률 분포)",
            "at": at.strftime("%Y-%m-%d %H시")}


# ------------------------------------------------------------------ 수요 · 공급
BASE_DEMAND = {"서울": 6, "경기": 8, "인천": 5, "충남": 4, "충북": 3, "대전": 3, "경북": 3, "대구": 3,
               "울산": 3, "부산": 5, "광주": 2, "강원": 1}
BASE_SUPPLY = {"서울": 7, "경기": 9, "인천": 5, "충남": 4, "충북": 3, "대전": 4, "경북": 3, "대구": 3,
               "울산": 3, "부산": 5, "광주": 3, "강원": 2}
SURGE_AT = 1.5
SURGE_MAX = 0.30


def poisson(lam: float, rng: random.Random) -> int:
    """포아송 분포 표본 (Knuth). λ 가 크면 정규 근사."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def demand_lambda(region: str, hour: int) -> float:
    return BASE_DEMAND.get(region, 2) * HOUR_PROFILE[hour]


def simulated_calls(region: str, at: datetime | None = None) -> int:
    """그 시간대의 가상 화물 콜 수 (포아송 · 같은 시간대엔 같은 값)."""
    at = at or datetime.now()
    return poisson(demand_lambda(region, at.hour), _rng("demand", region, at.strftime("%Y%m%d%H")))


def simulated_supply(region: str, at: datetime | None = None) -> int:
    at = at or datetime.now()
    base = BASE_SUPPLY.get(region, 2) * (0.8 if at.hour in RUSH_HOURS else 1.0)
    return max(1, round(base))


def surge_multiplier(ds_ratio: float) -> float:
    """D/S ≤ 1.5 → 0 (할증 없음). 넘으면 +10% 에서 시작해 비율이 1 오를 때마다 +10%, 최대 +30%."""
    if ds_ratio <= SURGE_AT:
        return 0.0
    return round(min(SURGE_MAX, 0.10 + 0.10 * (ds_ratio - SURGE_AT)), 3)


def market(region: str, real_demand: int = 0, real_supply: int = 0, at: datetime | None = None) -> dict:
    """권역 수급 지수 + 서지 할증 + 날씨."""
    at = at or datetime.now()
    demand = real_demand + simulated_calls(region, at)
    supply = real_supply + simulated_supply(region, at)
    ratio = round(demand / max(supply, 1), 2)
    return {"region": region, "demand": demand, "supply": supply, "ds_ratio": ratio,
            "real_demand": real_demand, "real_supply": real_supply,
            "surge": surge_multiplier(ratio), "surge_on": ratio > SURGE_AT, "rush_hour": at.hour in RUSH_HOURS,
            "lambda": round(demand_lambda(region, at.hour), 1), "weather": mock_get_weather_condition(region, at),
            "at": at.strftime("%H시")}
