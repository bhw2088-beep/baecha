"""
Phase 1: 물리적 적재 오탐 방지 규칙 (순수 함수 · pytest 로 독립 검증)

EP(Extreme Point) 엔진이 상자를 배치한 뒤, 그 결과를 '보수적으로' 한 번 더 검사한다.

1) 보수적 안전율 (기본 10%, config.safety_margin)
   실제 박스 포장 오차 · 측정 오차를 감안해 적재함 용량의 일부만 쓴 것으로 본다.
     부피 · 중량 사용률 > 85%  → 경고 "공간 부족(오탐) 위험"
     부피 · 중량 사용률 > 90%  → 차단 (1 − 안전율)
2) 축중량 균형 (산업안전보건기준에 관한 규칙 제173조 '화물의 편하중 방지' 취지)
   무게중심이 한쪽으로 쏠리면 축 하나에 하중이 몰린다.
     좌우 편차 > 10% 경고 · > 15% 차단
     전후 무게중심 위치(적재함 길이 대비) 20~65% 정상 · 15~75% 경고 · 그 밖은 차단
     단, 화물이 최대 적재량의 30% 미만으로 가벼우면 축 과하중 위험이 낮아 '차단' 대신 '경고'로 낮춘다.
   앞축 · 뒤축 하중은 '적재함 앞 15% 지점에 앞축, 70% 지점에 뒤축'인 단순 지렛대 모델로 계산한다.
3) 필수 최소 톤수 (required_tonnage)
   안전율을 적용한 상태에서 총중량 · 총부피를 담을 수 있는 가장 작은 표준 차량의 톤수
   → 매칭 1단계 하드 필터 '기사 톤수 ≥ 필수 최소 톤수'.
"""

from __future__ import annotations

WARN_RATIO = 0.85

# 축 위치 (적재함 길이 대비 비율, 0 = 운전석 쪽 끝)
FRONT_AXLE = -0.15
REAR_AXLE = 0.70


def capacity_limit(safety_margin: float) -> float:
    return round(1 - safety_margin, 4)


def safety_check(volume_util: float, weight_util: float, safety_margin: float = 0.10) -> dict:
    """부피 · 중량 사용률 → pass / warn / fail."""
    limit = capacity_limit(safety_margin)
    worst = max(volume_util, weight_util)
    which = "부피" if volume_util >= weight_util else "중량"
    if worst > limit + 1e-9:
        status = "fail"
        detail = f"{which} {worst:.0%} — 안전율 {safety_margin:.0%} 적용 한도 {limit:.0%} 초과 (더 큰 차량 필요)"
    elif worst > WARN_RATIO + 1e-9:
        status = "warn"
        detail = f"{which} {worst:.0%} — 공간 부족(오탐) 위험 구간 ({WARN_RATIO:.0%}~{limit:.0%})"
    else:
        status = "pass"
        detail = f"부피 {volume_util:.0%} · 중량 {weight_util:.0%} (한도 {limit:.0%})"
    return {"status": status, "detail": detail, "limit": limit, "warn_at": WARN_RATIO, "worst": round(worst, 4)}


def axle_loads(total_kg: float, cog_x_ratio: float) -> dict:
    """무게중심 위치(전후 비율) → 앞축 · 뒤축이 나눠 받는 화물 하중 (kg)."""
    span = REAR_AXLE - FRONT_AXLE
    rear_share = max(0.0, min(1.0, (cog_x_ratio - FRONT_AXLE) / span))
    return {"front_kg": round(total_kg * (1 - rear_share), 1), "rear_kg": round(total_kg * rear_share, 1),
            "front_ratio": round(1 - rear_share, 3), "rear_ratio": round(rear_share, 3)}


LIGHT_LOAD = 0.30


def axle_balance(rows: list[dict], length: int, width: int, max_weight: float | None = None) -> dict:
    """배치 결과(상자 좌표 · 무게) → 좌우 · 전후 무게중심과 축 하중 판정."""
    total = sum(r["weight_kg"] for r in rows)
    if total <= 0:
        return {"status": "pass", "detail": "적재 화물 없음", "lateral": 0.0, "longitudinal": None,
                **axle_loads(0, 0.5)}
    cx = sum((r["x"] + r["length"] / 2) * r["weight_kg"] for r in rows) / total
    cy = sum((r["y"] + r["width"] / 2) * r["weight_kg"] for r in rows) / total
    lat = abs(cy - width / 2) / width
    lon = cx / length
    if lat > 0.15 or not (0.15 <= lon <= 0.75):
        status = "fail"
    elif lat > 0.10 or not (0.20 <= lon <= 0.65):
        status = "warn"
    else:
        status = "pass"
    light = bool(max_weight) and total < max_weight * LIGHT_LOAD
    if status == "fail" and light:
        status = "warn"  # 가벼운 화물: 축 과하중 위험이 낮음
    loads = axle_loads(total, lon)
    side = "좌측" if cy < width / 2 else "우측"
    detail = (f"전후 {lon:.0%} · 좌우 편차 {lat:.1%}({side}) · 앞축 {loads['front_kg']:,.0f}kg / "
              f"뒤축 {loads['rear_kg']:,.0f}kg")
    if light and status != "pass":
        detail += f" · 경량 화물(최대 적재량의 {total / max_weight:.0%})"
    return {"status": status, "detail": detail, "lateral": round(lat, 4), "longitudinal": round(lon, 4),
            "side": side, "light_load": light, **loads}


def required_tonnage(total_kg: float, total_volume_mm3: float, trucks: list, safety_margin: float = 0.10,
                     cold_chain: bool = False) -> float | None:
    """안전율을 적용해 총중량 · 총부피를 담을 수 있는 가장 작은 표준 차량의 톤수 (없으면 None).
    trucks: Container 목록 (engines/packer/trucks.py)."""
    from .compliance_engine import tonnage
    limit = capacity_limit(safety_margin)
    ok = [t for t in trucks if (t.refrigerated or not cold_chain)
          and total_kg <= t.max_weight * limit and total_volume_mm3 <= t.volume * limit]
    return min((tonnage(t.name) for t in ok), default=None)
