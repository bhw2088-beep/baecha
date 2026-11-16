"""
Phase 3: 실시간 경로 이탈 · 지연 감지 엔진 (순수 함수 · pytest 로 독립 검증)

[경로 이탈]  GPS 한 점 ↔ 계획 경로(폴리라인) 최단 거리
  - Shapely LineString.distance 로 계산 (위경도를 그 지점 기준 km 평면으로 바꿔서).
    Shapely 가 없으면 같은 계산을 순수 파이썬(점-선분 거리)으로 한다.
  - 디바운싱: 기준 거리(기본 1km)를 '연속 3번' 넘었을 때 정확히 3번째에 한 번만 경고한다.
    GPS 튀김(한두 번 튀는 점)으로 경고가 울리는 오탐을 막는다. 경로로 돌아오면 횟수는 0 으로.

[지연]  Delay Delta = (계획 시작 후 흐른 시간 + 지금 남은 거리 ÷ 계획 속도) − 계획 시점의 남은 시간
  - 30분(기본)을 넘으면 관제 알림. 시뮬레이션 시간 배속(TIME_SCALE)을 반영한 '운행 시간' 기준.
"""

from __future__ import annotations

import math

PLAN_SPEED_KMH = 70.0  # 계획 ETA 기준 평균 속도

try:  # Shapely 2.x (도커 이미지에 설치) — 없으면 순수 파이썬 계산
    from shapely.geometry import LineString, Point
    HAS_SHAPELY = True
except Exception:  # noqa: BLE001
    HAS_SHAPELY = False


def _to_xy(p: tuple[float, float], lat0: float) -> tuple[float, float]:
    """(lat, lon) → 기준 위도 lat0 에서의 평면 좌표 (km). 수십 km 범위에서 오차 1% 미만."""
    return (p[1] * 111.320 * math.cos(math.radians(lat0)), p[0] * 110.574)


def _seg_dist(px, py, ax, ay, bx, by) -> tuple[float, float]:
    """점 P 와 선분 AB 의 거리 · 선분 위 투영 비율(0~1)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy), t


def locate(point: tuple[float, float], route: list[tuple[float, float]]) -> dict:
    """GPS 한 점을 계획 경로에 투영: 경로까지 거리(km) · 경로상 진행률(0~1)."""
    if len(route) < 2:
        return {"distance_km": 0.0, "progress": 0.0, "engine": "none"}
    lat0 = point[0]
    pts = [_to_xy(p, lat0) for p in route]
    px, py = _to_xy(point, lat0)
    if HAS_SHAPELY:
        line = LineString(pts)
        pt = Point(px, py)
        return {"distance_km": round(line.distance(pt), 3),
                "progress": round(line.project(pt) / line.length, 4) if line.length else 0.0, "engine": "shapely"}
    best, best_i, best_t = float("inf"), 0, 0.0
    for i in range(len(pts) - 1):
        d, t = _seg_dist(px, py, *pts[i], *pts[i + 1])
        if d < best:
            best, best_i, best_t = d, i, t
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(seg) or 1.0
    along = sum(seg[:best_i]) + seg[best_i] * best_t
    return {"distance_km": round(best, 3), "progress": round(along / total, 4), "engine": "python"}


def debounce(count: int, off_route: bool, hits: int = 3) -> tuple[int, str | None]:
    """연속 이탈 횟수 갱신 → (새 횟수, 이벤트). 이벤트: 'ALERT'(정확히 hits 번째) · 'RECOVERED' · None."""
    if off_route:
        count += 1
        return count, ("ALERT" if count == hits else None)
    return 0, ("RECOVERED" if count >= hits else None)


def delay_minutes(elapsed_min: float, remaining_km: float, planned_remaining_min: float,
                  plan_speed: float = PLAN_SPEED_KMH) -> int:
    """계획 대비 지연(분). 양수 = 늦음. elapsed_min: 계획 기준 시점 이후 흐른 운행 시간(분)."""
    return int(round(elapsed_min + remaining_km / plan_speed * 60 - planned_remaining_min))


def jitter(point: tuple[float, float], rng, sigma_m: float = 8.0) -> tuple[float, float]:
    """GPS 튀김 흉내: 위경도에 가우스 노이즈 (기본 표준편차 8m)."""
    dlat = rng.gauss(0, sigma_m) / 110_574
    dlon = rng.gauss(0, sigma_m) / (111_320 * math.cos(math.radians(point[0])))
    return point[0] + dlat, point[1] + dlon


def offset_point(route: list[tuple[float, float]], ratio: float, km: float) -> tuple[float, float]:
    """경로 위 ratio 지점에서 진행 방향의 오른쪽으로 km 만큼 벗어난 점 (고의 이탈 시나리오)."""
    from .matcher.spatial import along
    p, i = along(route, ratio)
    a, b = route[i], route[min(i + 1, len(route) - 1)]
    lat0 = p[0]
    ax, ay = _to_xy(a, lat0)
    bx, by = _to_xy(b, lat0)
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy) or 1.0
    ox, oy = dy / n * km, -dx / n * km  # 오른쪽 법선
    return p[0] + oy / 110.574, p[1] + ox / (111.320 * math.cos(math.radians(lat0)))
