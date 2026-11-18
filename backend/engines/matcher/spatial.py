"""
주소 → 좌표 변환과 거리 · 도로 경로 계산.

- 지오코딩: '주소 안에 들어있는 지역 키워드'로 대략적인 좌표를 찾는 간이 방식 (외부 API 없음).
- 도로 경로: 키워드 좌표 · 물류 거점의 모든 조합을 tools/build_routes.py 로 미리 받아 둔
  backend/data/route_cache.json 에서 읽는다 (OSRM · 오픈스트리트맵 기반, 실행 중 API 호출 없음).
- 캐시에 없는 지점(예: 운행 중 차량의 현재 위치)은 '가장 가까운 캐시 지점'에 붙인다:
    현재 위치 →(짧은 접속 구간, 직선 × 1.25)→ 가까운 지점 →(캐시된 도로 경로)→ … → 목적지
  그래서 기사 위치 → 상차지 거리도 항상 도로 경로 기준이다. 캐시 파일이 없을 때만 전체를 직선 × 1.25 로 추정.
"""

import json
import math
import os
import time

# (위도 lat, 경도 lon)  — 키워드가 길수록 먼저 매칭되도록 정렬해서 사용한다.
REGION_COORDS: dict[str, tuple[float, float]] = {
    "송도": (37.383, 126.656),
    "인천항": (37.463, 126.616),
    "인천": (37.456, 126.705),
    "가산": (37.478, 126.887),
    "금천": (37.457, 126.896),
    "종로": (37.582, 127.002),
    "서울": (37.566, 126.978),
    "파주": (37.760, 126.780),
    "문산": (37.856, 126.787),
    "탄현": (37.776, 126.716),
    "평택항": (36.970, 126.830),
    "포승": (36.952, 126.858),
    "평택": (36.992, 127.088),
    "동탄": (37.200, 127.074),
    "화성": (37.200, 127.074),
    "안성": (37.008, 127.280),
    "여주": (37.298, 127.637),
    "원주": (37.342, 127.920),
    "문막": (37.312, 127.817),
    "오송": (36.630, 127.326),
    "오창": (36.716, 127.430),
    "청주": (36.642, 127.489),
    "탕정": (36.789, 127.067),
    "아산": (36.790, 127.002),
    "당진": (36.890, 126.630),
    "송악": (36.932, 126.694),
    "대덕": (36.375, 127.360),
    "유성": (36.362, 127.356),
    "대전": (36.351, 127.385),
    "구미": (36.120, 128.344),
    "대구": (35.871, 128.601),
    "광주": (35.160, 126.851),
    "용봉": (35.177, 126.906),
    "양정": (35.570, 129.350),
    "삼남": (35.555, 129.110),
    "여천": (35.522, 129.350),
    "울산": (35.539, 129.311),
    "감전": (35.153, 128.983),
    "사상": (35.152, 128.991),
    "부산항": (35.100, 129.040),
    "부산": (35.180, 129.075),
}
_KEYS = sorted(REGION_COORDS, key=len, reverse=True)
DEFAULT_COORD = (36.351, 127.385)  # 못 찾으면 대전(국토 중앙)

# 화면 드롭다운에 보여 줄 지역 주소 → 좌표 키워드 (상차지 · 하차지 · 기사 대기 지역 선택용)
# 여기 있는 주소는 모두 도로 경로 캐시에 들어 있다. 새 지역을 넣으면 tools/build_routes.py 를 다시 실행.
REGION_LABELS: dict[str, str] = {
    "서울 금천구 가산동": "가산", "서울 금천구 독산동": "금천", "서울 종로구 대학로": "종로", "서울 중구": "서울",
    "인천 연수구 송도동": "송도", "인천 중구 인천항 물류센터": "인천항", "인천 남동구 남동공단": "인천",
    "경기 파주시 문산읍": "문산", "경기 파주시 탄현면": "탄현", "경기 파주시": "파주",
    "경기 평택항 물류단지": "평택항", "경기 평택시 포승읍": "포승", "경기 평택시": "평택",
    "경기 화성시 동탄": "동탄", "경기 안성시": "안성", "경기 여주시": "여주",
    "강원 원주시": "원주", "강원 원주시 문막읍": "문막",
    "충북 청주시 오송읍": "오송", "충북 청주시 오창": "오창", "충북 청주시": "청주",
    "충남 아산시 탕정면": "탕정", "충남 아산시": "아산", "충남 당진시": "당진", "충남 당진시 송악읍": "송악",
    "대전 대덕구 물류단지": "대덕", "대전 유성구": "유성", "대전 중구": "대전",
    "경북 구미시 공단동": "구미", "대구 북구 산격동": "대구",
    "광주 광산구": "광주", "광주 북구 용봉동": "용봉",
    "울산 북구 양정동": "양정", "울산 울주군 삼남": "삼남", "울산 남구 여천동": "여천", "울산 남구": "울산",
    "부산 사상구 감전동": "감전", "부산 사상구": "사상", "부산항": "부산항", "부산 연제구": "부산",
}


def region_options() -> list[str]:
    """드롭다운 선택지 (시 · 도 순)."""
    order = ["서울", "인천", "경기", "강원", "충북", "충남", "대전", "경북", "대구", "광주", "울산", "부산"]
    return sorted(REGION_LABELS, key=lambda s: (order.index(s.split()[0][:2]), s))


def is_known(address: str) -> bool:
    """좌표를 찾을 수 있는 주소인지 (못 찾으면 대전 좌표로 떨어진다)."""
    return address in REGION_LABELS or any(w.startswith(k) for w in address.split() for k in _KEYS)

# 주요 물류 거점 — 시스템 전체가 이 목록 하나를 쓴다
#   관제 지도 거점 표시 · 배경 차량 · 가상 화주 주문(simulator/virtual_shipper) · 오지 할증(billing) · 도로 경로 캐시
#   (좌표를 바꾸면 tools/build_routes.py 로 도로 경로 캐시를 다시 만들어야 한다)
HUBS = {
    "서울/인천": (37.50, 126.80),
    "평택항": (36.97, 126.83),
    "대전 HUB": (36.35, 127.38),
    "대구": (35.87, 128.60),
    "광주": (35.16, 126.85),
    "울산": (35.54, 129.31),
    "부산항": (35.10, 129.04),
}

# 거점별 화물 발생 비중 (가상 주문 · 배경 차량 배치에 사용, 합 1.0)
HUB_WEIGHTS = {
    "서울/인천": 0.36,
    "평택항": 0.16,
    "대전 HUB": 0.14,
    "부산항": 0.14,
    "울산": 0.08,
    "대구": 0.06,
    "광주": 0.06,
}

# 배경 차량이 오가는 간선 축 (거점 이름 쌍)
CORRIDORS = [
    ("서울/인천", "평택항"),
    ("평택항", "대전 HUB"),
    ("서울/인천", "대전 HUB"),
    ("대전 HUB", "대구"),
    ("대구", "부산항"),
    ("대구", "울산"),
    ("대전 HUB", "광주"),
    ("울산", "부산항"),
]


# 공차 기사의 실시간 위치 (POST /tracking/location) 를 이 시간 동안만 믿는다 — 끊기면 등록 지역(location)으로 돌아감
LIVE_TTL_SEC = 600


def driver_point(d: dict) -> tuple[float, float]:
    """기사 대기 위치: 최근에 보고한 실시간 위치(공차 텔레메트리)가 있으면 그 좌표, 없으면 등록 지역 좌표.
    매칭 거리 · PostGIS 반경 필터(drivers.geom) · 관제 지도가 모두 이 함수를 쓴다."""
    live = d.get("live")
    if live and time.time() - live.get("t", 0) <= LIVE_TTL_SEC:
        return live["lat"], live["lon"]
    return geocode(d["location"])


def geocode(address: str) -> tuple[float, float]:
    """주소 → 좌표.
    1) 드롭다운 주소면 그대로  2) 단어 '앞부분'이 키워드인 것 중 가장 뒤 단어(가장 구체적인 지역), 같으면 긴 키워드
    3) 그래도 없으면 주소 어디든 들어 있는 키워드.  (예: '구미 국가산업단지' 의 '가산' 은 단어 중간이라 무시)"""
    if address in REGION_LABELS:
        return REGION_COORDS[REGION_LABELS[address]]
    for word in reversed(address.split()):
        for key in _KEYS:
            if word.startswith(key):
                return REGION_COORDS[key]
    for key in _KEYS:
        if key in address:
            return REGION_COORDS[key]
    return DEFAULT_COORD


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


ROAD_FACTOR = 1.25  # 직선거리 → 도로거리 보정 계수 (도로 경로 캐시가 없을 때만 사용)

# ---------------------------------------------------------------- 도로 경로 캐시
# backend/data/route_cache.json (이 파일은 backend/engines/matcher/ 에 있음)
ROUTE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "data", "route_cache.json")
_routes: dict[str, dict] = {}
_decoded: dict[str, list[tuple[float, float]]] = {}
_nodes: list[tuple[float, float]] = []  # 캐시에 들어 있는 지점(경로 양 끝) 목록
_loaded = {"mtime": None, "checked": 0.0}
RELOAD_CHECK_SEC = 30  # 캐시 파일이 바뀌면(경로를 더 받은 경우) 서버 재시작 없이 다시 읽는다


def coord_key(p: tuple[float, float]) -> str:
    return f"{p[0]:.4f},{p[1]:.4f}"


def load_routes():
    """서버 시작 시 1회 읽는다. 파일이 없으면 모든 경로를 직선으로 처리."""
    global _routes, _nodes
    _decoded.clear()
    _loaded["checked"] = time.time()
    try:
        _loaded["mtime"] = os.path.getmtime(ROUTE_FILE)
        with open(ROUTE_FILE, encoding="utf-8") as f:
            _routes = json.load(f)
    except (OSError, ValueError):
        _routes = {}
    keys = {k for pair in _routes for k in pair.split("|")}
    _nodes = [tuple(map(float, k.split(","))) for k in sorted(keys)]


def _maybe_reload():
    now = time.time()
    if now - _loaded["checked"] < RELOAD_CHECK_SEC:
        return
    _loaded["checked"] = now
    try:
        if os.path.getmtime(ROUTE_FILE) != _loaded["mtime"]:
            load_routes()
    except OSError:
        pass


def route_count() -> int:
    return len(_routes)


def _decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    """Google/OSRM encoded polyline → [(lat, lon), ...]"""
    coords, index, lat, lon, factor = [], 0, 0, 0, 10 ** precision
    while index < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    """[(lat, lon), ...] → Google/OSRM encoded polyline (화물 planned_polyline 저장용, 1/10만 도 ≈ 1m 정밀도)."""
    factor, out, prev_lat, prev_lon = 10 ** precision, [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(s: str) -> list[tuple[float, float]]:
    """저장된 planned_polyline → [(lat, lon), ...] (같은 문자열은 한 번만 풀어 둔다)."""
    if s not in _decoded:
        _decoded[s] = _decode_polyline(s)
    return _decoded[s]


def _cached(a: tuple[float, float], b: tuple[float, float]) -> tuple[dict | None, bool]:
    _maybe_reload()
    ka, kb = coord_key(a), coord_key(b)
    if ka <= kb:
        return _routes.get(f"{ka}|{kb}"), False
    return _routes.get(f"{kb}|{ka}"), True


def nearest_node(p: tuple[float, float]) -> tuple[float, float] | None:
    """캐시에 들어 있는 지점 중 p 에서 가장 가까운 곳 (캐시가 없으면 None)."""
    _maybe_reload()
    return min(_nodes, key=lambda n: haversine_km(p, n)) if _nodes else None


def _direct(a: tuple[float, float], b: tuple[float, float]) -> tuple[list[tuple[float, float]], float] | None:
    """캐시에 a–b 조합이 그대로 있으면 (경로, km)."""
    if coord_key(a) == coord_key(b):
        return [a, b], 0.0
    hit, reverse = _cached(a, b)
    if not hit:
        return None
    key = hit["poly"]
    if key not in _decoded:
        _decoded[key] = _decode_polyline(key)
    pts = _decoded[key][::-1] if reverse else _decoded[key]
    return ([a, *pts[1:-1], b] if len(pts) > 2 else [a, b]), hit["km"]  # 양 끝은 지역 좌표에 맞춤


def _snapped(a: tuple[float, float], b: tuple[float, float]) -> tuple[list[tuple[float, float]], float]:
    """a → b 도로 경로와 거리. 캐시에 없는 끝점은 가장 가까운 캐시 지점까지 짧은 접속 구간으로 잇는다."""
    hit = _direct(a, b)
    if hit:
        return hit
    na, nb = nearest_node(a), nearest_node(b)
    mid = _direct(na, nb) if na and nb else None
    if not mid:  # 캐시 파일이 없음
        return [a, b], round(haversine_km(a, b) * ROAD_FACTOR, 1)
    lead = haversine_km(a, na) * ROAD_FACTOR
    tail = haversine_km(nb, b) * ROAD_FACTOR
    if lead + tail >= haversine_km(a, b) * ROAD_FACTOR:
        # 두 끝이 같은 캐시 지점에 붙는 짧은 거리 (예: 같은 동네) → 접속 구간이 더 길어지므로 직접 잇는다
        return [a, b], round(haversine_km(a, b) * ROAD_FACTOR, 1)
    path = [a, *mid[0], b]
    return path, round(lead + mid[1] + tail, 1)


def route(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    """a → b 도로 경로 [(lat, lon), ...]."""
    return _snapped(a, b)[0]


def route_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """a → b 도로 거리 (km). 캐시 지점끼리는 실제 도로 거리, 그 밖의 지점은 가까운 캐시 지점 경유."""
    return _snapped(a, b)[1]


def road_km(origin: str, destination: str) -> float:
    return round(route_km(geocode(origin), geocode(destination)), 1)


def interpolate(a: tuple[float, float], b: tuple[float, float], ratio: float) -> tuple[float, float]:
    ratio = max(0.0, min(1.0, ratio))
    return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)


def along(path: list[tuple[float, float]], ratio: float) -> tuple[tuple[float, float], int]:
    """경로를 따라 ratio(0~1) 지점의 좌표와, 그 지점이 속한 구간 번호."""
    ratio = max(0.0, min(1.0, ratio))
    seg = [haversine_km(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg)
    if total == 0:
        return path[0], 0
    target, acc = total * ratio, 0.0
    for i, s in enumerate(seg):
        if acc + s >= target:
            return interpolate(path[i], path[i + 1], (target - acc) / s if s else 0), i
        acc += s
    return path[-1], len(seg) - 1


def offset_perpendicular(a, b, point, amount_deg: float):
    """경로 이탈 차량을 경로 옆으로 살짝 밀어서 그리기 위한 함수."""
    dlat, dlon = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dlat, dlon) or 1.0
    return (point[0] - dlon / norm * amount_deg, point[1] + dlat / norm * amount_deg)


load_routes()


# ---------------------------------------------------------------- 매칭 1단계: PostGIS 반경 필터
def drivers_within(origin: tuple[float, float], radius_km: float) -> set[str] | None:
    """PostGIS ST_DWithin: 대기 위치가 상차지에서 반경(직선거리) 안에 있는 기사 id.

    도로거리는 항상 직선거리보다 길므로, 여기서 걸러진 기사만 2단계에서 도로거리 · 적재 시뮬레이션을 계산하면 된다.
    DB 를 쓸 수 없으면(테스트 · 로컬 메모리 실행) None → 호출한 쪽이 직선 필터 없이 진행한다.
    """
    try:
        from sqlalchemy import text

        from ...core.database import SessionLocal
        with SessionLocal() as s:
            rows = s.execute(text("SELECT id FROM drivers "
                                  "WHERE ST_DWithin(geom, ST_GeogFromText(:p), :m)"),
                             {"p": f"SRID=4326;POINT({origin[1]:.6f} {origin[0]:.6f})", "m": radius_km * 1000})
            return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return None
