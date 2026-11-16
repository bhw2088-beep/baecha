"""
디자인 시스템: 색상 토큰, 전역 CSS, 재사용 컴포넌트(헤더/KPI/상태 배지/경로 바), 포맷터.

모든 화면은 여기 있는 함수로만 꾸민다 → 디자인을 바꿀 때 이 파일만 고치면 된다.
"""

import os
from datetime import datetime
from html import escape

import pandas as pd
import pydeck as pdk
import streamlit as st

import api

# ---------------------------------------------------------------- 색상 토큰
INK = "#0E1116"
MUTED = "#667085"
PAPER = "#F7F8FA"
LINE = "#E4E7EC"
BRAND = "#4F46E5"   # indigo

TONES = {  # 상태/의미별 색 (전경, 배경)
    "brand": ("#4338CA", "#EEF2FF"),
    "blue": ("#1D4ED8", "#EFF4FF"),
    "green": ("#067647", "#ECFDF3"),
    "teal": ("#0E7490", "#ECFEFF"),
    "amber": ("#B54708", "#FFFAEB"),
    "red": ("#B42318", "#FEF3F2"),
    "gray": ("#475467", "#F2F4F7"),
}
STATUS_TONE = {
    "MATCHING": "amber", "ASSIGNED": "gray", "IN_TRANSIT": "blue", "DELIVERED": "teal", "SETTLED": "green",
    "UNBILLED": "brand", "REQUESTED": "amber", "PAID": "green", "REFUNDED": "red",
    "UNSOLVED": "red", "RESOLVED": "green", "HIGH": "red", "MEDIUM": "amber", "LOW": "blue",
}
# 지도 색 [R,G,B]
MAP_COLORS = {
    "transit": [59, 130, 246], "cold": [20, 184, 166], "empty": [34, 197, 94], "anomaly": [239, 68, 68],
}
KIND_LABEL = {"transit": "운행중", "cold": "콜드체인", "empty": "공차 대기", "anomaly": "이상 감지"}
LOGO = '<span class="ct-logo"><span class="ct-mark"></span>배차의 민족</span>'

# 전역 CSS 는 static/style.css 에 있다 (정적 리소스)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
with open(os.path.join(STATIC_DIR, "style.css"), encoding="utf-8") as _f:
    CSS = "<style>" + _f.read() + "</style>"


def inject_css():
    st.html(CSS)


# ---------------------------------------------------------------- 포맷터
def won(n: int | float | None) -> str:
    return "-" if n is None else f"{int(n):,}원"


def manwon(n: int | float | None) -> str:
    """큰 금액 요약 (예: 1,942,000 → 194.2만)"""
    if n is None:
        return "-"
    return f"{n / 10_000:,.1f}만" if n >= 100_000 else f"{int(n):,}"


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분 {s:02d}초" if m else f"{s}초"


def minutes_text(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    h, m = divmod(minutes, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def dt_text(iso: str | None, fmt: str = "%m.%d %H:%M") -> str:
    return datetime.fromisoformat(iso).strftime(fmt) if iso else "-"


def ago(iso: str | None) -> str:
    if not iso:
        return "-"
    sec = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    if sec < 60:
        return "방금 전"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 전"
    return f"{int(sec // 86400)}일 전"


def temp_text(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}°C"


# ---------------------------------------------------------------- 컴포넌트 (HTML 문자열)
def pill(text: str, tone: str = "gray", dot: bool = True) -> str:
    fg, bg = TONES.get(tone, TONES["gray"])
    return f'<span class="ct-pill" style="color:{fg};background:{bg}">{"<i></i>" if dot else ""}{escape(str(text))}</span>'


def status_pill(order: dict) -> str:
    if order.get("anomaly"):
        return pill(f"이상감지 · {order['anomaly']['text']}", "red")
    return pill(order["status_label"], STATUS_TONE.get(order["status"], "gray"))


def html(markup: str):
    st.markdown(f'<div class="ct">{markup}</div>', unsafe_allow_html=True)


def page_header(eyebrow: str, title: str, desc: str = ""):
    html(f'<div class="ct-head"><div><div class="ct-eyebrow">{escape(eyebrow)}</div>'
         f'<div class="ct-title">{escape(title)}</div><div class="ct-desc">{escape(desc)}</div></div></div>')


def kpis(items: list[dict]):
    """items: [{label, value, unit?, note?, alert?}]"""
    cells = "".join(
        f'<div class="ct-kpi{" alert" if it.get("alert") else ""}"><div class="l">{escape(it["label"])}</div>'
        f'<div class="v">{escape(str(it["value"]))}<small>{escape(it.get("unit", ""))}</small></div>'
        f'<div class="n">{escape(it.get("note", ""))}</div></div>'
        for it in items
    )
    html(f'<div class="ct-kpis" style="--n:{len(items)}">{cells}</div>')


def section(title: str, aside: str = ""):
    html(f'<div class="ct-sec"><b>{escape(title)}</b><span>{escape(aside)}</span></div>')


def route_bar(origin: str, destination: str, progress: float, left_meta: str = "", right_meta: str = "") -> str:
    p = max(0.0, min(100.0, progress or 0))
    return (f'<div class="ct-route"><div class="ends"><span><small>상차</small>{escape(origin)}</span>'
            f'<span><small>하차</small>{escape(destination)}</span></div>'
            f'<div class="ct-track"><div class="fill" style="width:{p}%"></div><div class="dot" style="left:{p}%"></div></div>'
            f'<div class="meta"><span>{escape(left_meta)}</span><span>{escape(right_meta)}</span></div></div>')


def stats(items: list[tuple[str, str]]) -> str:
    return '<div class="ct-stats">' + "".join(
        f'<div class="ct-stat"><div class="l">{escape(l)}</div><div class="v">{escape(v)}</div></div>' for l, v in items
    ) + "</div>"


def fare_compare(fe: dict | None) -> str:
    """제시 운임 vs AI 적정 운임 (도로거리 · 차종 · 중량 기준) 한 줄 요약."""
    if not fe:
        return ""
    gap = fe["gap_ratio"]
    tone = "green" if gap >= 0.03 else "red" if gap <= -0.07 else "gray"
    word = (f"적정가보다 {gap * 100:.0f}% 높음" if gap >= 0.03 else
            f"적정가보다 {-gap * 100:.0f}% 낮음" if gap <= -0.03 else "적정가 수준")
    extra = " · ".join(f'{f["label"]} +{f["amount"]:,}' for f in fe["factors"])
    return (f'<div class="ct-fare"><span>AI 적정 운임 <b>{won(fe["recommended"])}</b> '
            f'<span class="ct-muted">· 도로 {fe["distance_km"]:.0f}km × {fe["per_km"]:,}원/km'
            f'{" · " + escape(extra) if extra else ""} · 제시 운임 km당 {fe["fare_per_km"]:,}원</span></span>'
            f'{pill(word, tone)}</div>')


def banner(text: str, tone: str = "red") -> str:
    fg, bg = TONES[tone]
    return f'<div class="ct-banner" style="color:{fg};background:{bg}">{escape(text)}</div>'


def legend() -> str:
    return '<div class="ct-legend">' + "".join(
        f'<span><i style="background:rgb({",".join(map(str, MAP_COLORS[k]))})"></i>{v}</span>'
        for k, v in KIND_LABEL.items()
    ) + '<span><i style="background:transparent;box-shadow:inset 0 0 0 2px #0E1116"></i>등록 차주</span>' \
        '<span style="margin-left:auto">차량에 마우스를 올리면 상세 정보</span></div>'


# ---------------------------------------------------------------- 지도
def _view_for(points: list[list[float]]) -> pdk.ViewState:
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.8)
    zoom = 8.6 if span < 0.3 else 7.8 if span < 0.6 else 7.1 if span < 1.2 else 6.7 if span < 2.0 else 6.4
    return pdk.ViewState(latitude=(max(lats) + min(lats)) / 2, longitude=(max(lons) + min(lons)) / 2, zoom=zoom)


def _split_route(route: list[list[float]], lon: float, lat: float) -> tuple[list, list]:
    """현재 위치에서 가장 가까운 경로 점을 기준으로 '지나온 길 / 남은 길'로 나눈다."""
    i = min(range(len(route)), key=lambda k: (route[k][0] - lon) ** 2 + (route[k][1] - lat) ** 2)
    return route[:i + 1] + [[lon, lat]], [[lon, lat]] + route[i + 1:]


def route_map(order: dict, height: int = 240):
    """화물 1건의 상차→하차 도로 경로와 현재 위치. 지나온 길은 진하게, 남은 길은 연하게."""
    route = order["route"]
    ends = pd.DataFrame([{"lon": route[0][0], "lat": route[0][1], "c": [14, 17, 22]},
                         {"lon": route[-1][0], "lat": route[-1][1], "c": [14, 17, 22]}])
    truck = pd.DataFrame([{"lon": order["lon"], "lat": order["lat"]}])
    truck_color = MAP_COLORS["anomaly"] if order.get("anomaly") else [79, 70, 229]
    done, left = _split_route(route, order["lon"], order["lat"]) if order["status"] == "IN_TRANSIT" else ([], route)
    layers = [
        pdk.Layer("PathLayer", data=pd.DataFrame([{"path": left}]), get_path="path", get_color=[79, 70, 229, 90],
                  width_min_pixels=4, joint_rounded=True, cap_rounded=True),
        pdk.Layer("PathLayer", data=pd.DataFrame([{"path": done}]) if len(done) > 1 else pd.DataFrame({"path": []}),
                  get_path="path", get_color=[79, 70, 229, 230], width_min_pixels=4, joint_rounded=True,
                  cap_rounded=True),
        pdk.Layer("ScatterplotLayer", data=ends, get_position="[lon, lat]", get_fill_color="c",
                  get_radius=3000, radius_min_pixels=5),
        pdk.Layer("ScatterplotLayer", data=truck, get_position="[lon, lat]", get_fill_color=truck_color,
                  get_radius=6000, radius_min_pixels=8, stroked=True, get_line_color=[255, 255, 255],
                  line_width_min_pixels=3),
    ]
    st.pydeck_chart(pdk.Deck(layers=layers, map_style=pdk.map_styles.CARTO_LIGHT, initial_view_state=_view_for(route)),
                    height=height)


def fleet_routes_map(orders: list[dict], height: int = 420, key: str | None = None):
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
        pdk.Layer("PathLayer", data=df, get_path="path", get_color="line", width_min_pixels=3,
                  joint_rounded=True, cap_rounded=True, pickable=True),
        pdk.Layer("ScatterplotLayer", data=df, get_position="[lon, lat]", get_fill_color="color",
                  get_radius=5000, radius_min_pixels=7, stroked=True, get_line_color=[255, 255, 255],
                  line_width_min_pixels=2, pickable=True),
    ]
    pts = [p for o in active for p in o["route"]]
    deck = pdk.Deck(layers=layers, map_style=pdk.map_styles.CARTO_LIGHT, initial_view_state=_view_for(pts),
                    tooltip={"html": "<b>{title}</b><br/>{id} · {status} {progress}<br/>{driver}",
                             "style": {"backgroundColor": "#0E1116", "color": "#F7F8FA", "fontSize": "12px",
                                       "borderRadius": "8px", "padding": "8px 10px"}})
    st.pydeck_chart(deck, height=height, key=key)


# ---------------------------------------------------------------- 상태/알림
def flash(message: str, icon: str = "✅"):
    """다음 렌더링에서 토스트로 보여줄 메시지 (st.rerun 이후에도 유지)."""
    st.session_state.setdefault("_flash", []).append((message, icon))


def show_flash():
    for message, icon in st.session_state.pop("_flash", []):
        st.toast(message, icon=icon)


def load(fn, *args, **kwargs):
    """API 호출 실패 시 화면에 오류를 보여주고 렌더링을 멈춘다."""
    try:
        return fn(*args, **kwargs)
    except api.ApiError as e:
        if e.status == 401:
            st.rerun()
        st.error(str(e), icon="⚠️")
        st.stop()


def act(fn, *args, success: str | None = None, **kwargs):
    """버튼 액션용: 성공하면 flash + True, 실패하면 오류 표시 + False."""
    try:
        result = fn(*args, **kwargs)
    except api.ApiError as e:
        st.error(str(e), icon="⚠️")
        return None
    if success:
        flash(success)
    return result if result is not None else True
