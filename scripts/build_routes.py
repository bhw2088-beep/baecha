"""
도로 경로 캐시 만들기 (한 번만 실행 · 인터넷 필요).

geo.py 의 지역 키워드 좌표 + 관제 지도 물류 거점(HUBS) 모든 조합에 대해
OSRM(오픈스트리트맵 기반 경로 계산) 공개 서버에서 도로 경로와 도로 거리를 받아
backend/src/data/route_cache.json 에 저장한다. 서버 실행 중에는 이 파일만 읽는다 (API 호출 없음).

    저장소 최상위 폴더에서:  python scripts/build_routes.py

- 이미 받은 조합은 건너뛴다 → geo.py 에 지역 키워드를 추가한 뒤 다시 실행하면 새 조합만 받는다.
- 공개 서버 이용 규칙(초당 1회 수준)을 지키도록 두 서버를 번갈아 천천히 호출한다.
- 화물차 통행 제한(높이·중량)은 반영되지 않는 일반 차량 경로다.
"""

import json
import os
import sys
import time
from itertools import combinations

import requests

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)  # backend/src 패키지
from src.engines.matcher import spatial as geo  # noqa: E402

SERVERS = [
    "https://router.project-osrm.org/route/v1/driving",
    "https://routing.openstreetmap.de/routed-car/route/v1/driving",
]
HEADERS = {"User-Agent": "baecha-route-builder"}
PAUSE = 1.1  # 서버당 요청 간격(초)


def main():
    points = {geo.coord_key(p): p for p in [*geo.REGION_COORDS.values(), geo.DEFAULT_COORD, *geo.HUBS.values()]}
    cache = {}
    if os.path.exists(geo.ROUTE_FILE):
        with open(geo.ROUTE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    pairs = [(a, b) for a, b in combinations(sorted(points), 2) if f"{a}|{b}" not in cache]
    print(f"지점 {len(points)}곳 · 받을 조합 {len(pairs)}개 (이미 저장 {len(cache)}개)")
    os.makedirs(os.path.dirname(geo.ROUTE_FILE), exist_ok=True)
    last = [0.0] * len(SERVERS)
    for n, (ka, kb) in enumerate(pairs, start=1):
        a, b = points[ka], points[kb]
        url_tail = f"/{a[1]},{a[0]};{b[1]},{b[0]}?overview=simplified&geometries=polyline"
        for attempt in range(6):
            i = (n + attempt) % len(SERVERS)
            wait = PAUSE - (time.time() - last[i])
            if wait > 0:
                time.sleep(wait)
            last[i] = time.time()
            try:
                r = requests.get(SERVERS[i] + url_tail, headers=HEADERS, timeout=20)
                data = r.json()
                if r.ok and data.get("code") == "Ok":
                    route = data["routes"][0]
                    cache[f"{ka}|{kb}"] = {"km": round(route["distance"] / 1000, 1), "poly": route["geometry"]}
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(2)
        else:
            print(f"  실패: {ka} → {kb} (직선으로 대체됨)")
        if n % 25 == 0 or n == len(pairs):
            with open(geo.ROUTE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
            print(f"  {n}/{len(pairs)} 저장")
    print(f"완료 · {geo.ROUTE_FILE} ({len(cache)}개 경로)")


if __name__ == "__main__":
    main()
