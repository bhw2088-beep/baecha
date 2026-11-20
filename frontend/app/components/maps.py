"""지도 컴포넌트 (pydeck): 화물 1건 경로 지도 · 여러 화물 운행 지도."""

import pandas as pd
import pydeck as pdk
import streamlit as st

from utils.theme import MAP_COLORS


def _view_for(points: list[list[float]]) -> pdk.ViewState:
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.8)
    zoom = 8.6 if span < 0.3 else 7.8 if span < 0.6 else 7.1 if span < 1.2 else 6.7 if span < 2.0 else 6.4
    return pdk.ViewState(latitude=(max(lats) + min(lats)) / 2, longitude=(max(lons) + min(lons)) / 2, zoom=zoom)


def _split_route(route: list[list[float]], lon: float, lat: float) -> tuple[list, list]:
    """현재 위치에서 가장 가까운 경로 점을 기준으로 '지나온 길 / 남은 길'로 나눈다."""
    i = min(range(len(route)), key=lambda k: (route[k][0] - lon) ** 2 + (route[k][1] - lat) ** 2)
    return route[:i + 1] + [[lon, lat]], [[lon, lat]] + route[i + 1:]


# 지도 차량 점: 작은 반경 + 최소 · 최대 픽셀 (지역이 보이게) · 갱신 사이를 미끄러지듯 이동 (deck.gl transitions)
# pydeck 은 radius_units 같은 '문자열' 설정을 계산식으로 바꿔 지도를 깨뜨리므로 단위 문자열은 쓰지 않는다
MAP_GLIDE_MS = 2000


def route_map(order: dict, height: int = 240, glide_ms: int = MAP_GLIDE_MS):
    """화물 1건의 상차→하차 도로 경로와 현재 위치. 지나온 길은 진하게, 남은 길은 연하게.
    glide_ms: 화면 갱신 주기와 같게 주면 차량 점이 갱신 사이를 끊김 없이 이동한다."""
    route = order["route"]
    ends = pd.DataFrame([{"lon": route[0][0], "lat": route[0][1], "c": [14, 17, 22]},
                         {"lon": route[-1][0], "lat": route[-1][1], "c": [14, 17, 22]}])
    truck = pd.DataFrame([{"lon": order["lon"], "lat": order["lat"]}])
    truck_color = MAP_COLORS["anomaly"] if order.get("anomaly") else [79, 70, 229]
    done, left = _split_route(route, order["lon"], order["lat"]) if order["status"] == "IN_TRANSIT" else ([], route)
    layers = [
        pdk.Layer("PathLayer", id="route_left", data=pd.DataFrame([{"path": left}]), get_path="path",
                  get_color=[79, 70, 229, 90], width_min_pixels=3, joint_rounded=True, cap_rounded=True),
        pdk.Layer("PathLayer", id="route_done",
                  data=pd.DataFrame([{"path": done}]) if len(done) > 1 else pd.DataFrame({"path": []}),
                  get_path="path", get_color=[79, 70, 229, 230], width_min_pixels=3, joint_rounded=True,
                  cap_rounded=True),
        pdk.Layer("ScatterplotLayer", id="route_ends", data=ends, get_position="[lon, lat]", get_fill_color="c",
                  get_radius=1500, radius_min_pixels=3.5, radius_max_pixels=6),
        pdk.Layer("ScatterplotLayer", id="route_truck", data=truck, get_position="[lon, lat]",
                  get_fill_color=truck_color, get_radius=2000, radius_min_pixels=5, radius_max_pixels=8,
                  stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1.5,
                  transitions={"getPosition": glide_ms}),
    ]
    st.pydeck_chart(pdk.Deck(layers=layers, map_style=pdk.map_styles.CARTO_LIGHT, initial_view_state=_view_for(route)),
                    height=height)


def fleet_routes_map(orders: list[dict], height: int = 420, key: str | None = None, glide_ms: int = MAP_GLIDE_MS):
    """여러 화물의 도로 경로와 차량 위치를 한 지도에 (화주 '운행 중 차량 지도')."""
    active = [o for o in orders if o["status"] in ("ASSIGNED", "IN_TRANSIT")]
    if not active:
        st.info("운행 중인 차량이 없습니다.")
        return
    rows = []
    for o in active:
        color = MAP_COLORS["anomaly"] if o.get("anomaly") else MAP_COLORS["cold"] if o["cold_chain"] \
            else MAP_COLORS["transit"] if o["status"] == "IN_TRANSIT" else [148, 163, 184]
        d = o.get("driver") or {}
        rows.append({"path": o["route"], "lon": o["lon"], "lat": o["lat"], "color": color + [255],
                     "line": color + [120], "title": o["title"], "id": o["id"],
                     "driver": f"{d.get('name', '-')} · {d.get('plate', '')}", "status": o["status_label"],
                     "progress": f"{o['progress']:.0f}%"})
    df = pd.DataFrame(rows)
    layers = [
        pdk.Layer("PathLayer", id="fleet_routes", data=df, get_path="path", get_color="line", width_min_pixels=2,
                  joint_rounded=True, cap_rounded=True, pickable=True),
        pdk.Layer("ScatterplotLayer", id="fleet_trucks", data=df, get_position="[lon, lat]", get_fill_color="color",
                  get_radius=1500, radius_min_pixels=4, radius_max_pixels=7, stroked=True,
                  get_line_color=[255, 255, 255], line_width_min_pixels=1, pickable=True,
                  transitions={"getPosition": glide_ms}),
    ]
    pts = [p for o in active for p in o["route"]]
    deck = pdk.Deck(layers=layers, map_style=pdk.map_styles.CARTO_LIGHT, initial_view_state=_view_for(pts),
                    tooltip={"html": "<b>{title}</b><br/>{id} · {status} {progress}<br/>{driver}",
                             "style": {"backgroundColor": "#0E1116", "color": "#F7F8FA", "fontSize": "12px",
                                       "borderRadius": "8px", "padding": "8px 10px"}})
    st.pydeck_chart(deck, height=height, key=key)
