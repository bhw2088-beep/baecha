"""
차주 적재 가이드: 화물에 저장된 적재 계획(EP 결과)을 현장 작업 순서로 보여준다.

- 상차 순서: 상자 하나씩 '어디에 · 어떤 방향으로 · 무엇 위에' 놓는지 문장으로 안내 + 3D
- 하차 순서: 하차지별로 내릴 상자를 강조 (먼저 내린 하차지 화물은 빠진 상태, 남은 화물은 회색)

좌표: x = 운전석 쪽 벽에서의 거리, y = 왼쪽 벽(운전석에 앉아 앞을 볼 때 왼쪽)에서의 거리, z = 바닥에서의 높이. 단위 mm.
서버는 차주의 실제 차량이 화주가 계획한 차량과 다르면 같은 품목으로 다시 계산한 계획을 돌려준다.
"""

from html import escape

import pandas as pd
import streamlit as st

from utils import api
from . import pack_3d
from .badges import pill
from .layout import banner, html, section, stats

EPS = 1


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0) > EPS


def _below(r: dict, rows: list[dict]) -> list[dict]:
    """r 바로 아래에서 r 을 받치는 상자들 (먼저 놓인 상자 중 윗면 높이 = r 바닥 높이)."""
    return [q for q in rows if abs(q["z"] + q["height"] - r["z"]) <= EPS
            and _overlap(q["x"], q["x"] + q["length"], r["x"], r["x"] + r["length"])
            and _overlap(q["y"], q["y"] + q["width"], r["y"], r["y"] + r["width"])]


def where_text(r: dict, truck: dict, before: list[dict]) -> list[str]:
    W = truck["width"]
    lines = []
    lines.append("운전석 쪽 벽에 붙여서" if r["x"] == 0 else f"운전석 쪽 벽에서 {r['x']:,}mm 뒤")
    if r["y"] == 0:
        lines.append("왼쪽 벽에 붙여서")
    elif abs(r["y"] + r["width"] - W) <= EPS:
        lines.append("오른쪽 벽에 붙여서")
    else:
        lines.append(f"왼쪽 벽에서 {r['y']:,}mm 띄워서")
    if r["z"] == 0:
        lines.append("바닥에 내려놓기")
    else:
        under = _below(r, before)
        by_name: dict[str, list[str]] = {}
        for q in under:
            by_name.setdefault(q["name"].split("·")[-1].strip(), []).append(q["item_id"])
        target = ", ".join(f"{nm} ({ids[0]}{f' 외 {len(ids) - 1}개' if len(ids) > 1 else ''})"
                           for nm, ids in by_name.items())
        lines.append(f"{target} 위에 올리기 (바닥에서 {r['z']:,}mm)")
    return lines


def orientation_text(r: dict) -> str | None:
    l, w, h = r["orig"]
    if r["height"] != h:
        return f"눕혀서 적재 — 원래 높이 {h:,}mm → {r['height']:,}mm"
    if (r["length"], r["width"]) != (l, w):
        return "바닥에서 90° 돌려서 적재 (길이·폭 방향 바뀜)"
    return None


def _tags(r: dict) -> str:
    tags = [pill(f"{r['drop']}차 하차", "blue")]
    if r["upright"]:
        tags.append(pill("세움 고정 ↑", "gray"))
    if not r["stackable"]:
        tags.append(pill("파손주의 · 위 적재 금지", "red"))
    elif r.get("max_top_load") is not None:
        tags.append(pill(f"상부 허용 {r['max_top_load']:,.0f}kg", "amber"))
    return " ".join(tags)


def _door_text(r: dict, truck: dict) -> str:
    gap = truck["length"] - r["x"] - r["length"]
    level = "바닥" if r["z"] == 0 else f"높이 {r['z']:,}mm"
    return f"{'뒷문 바로 앞' if gap == 0 else f'뒷문에서 {gap:,}mm 안쪽'} · {level}"


def _move(key: str, delta: int, n: int):
    st.session_state[key] = min(n, max(1, st.session_state.get(key, 1) + delta))


# ======================================================================= main
def render(order_id: str, key: str, height: int = 520):
    try:
        g = api.get(f"/orders/{order_id}/packing")
    except api.ApiError as e:
        st.info(str(e) if e.status != 404 else "이 화물에는 적재 계획이 없습니다.")
        return
    truck, rows, m = g["truck"], g["placements"], g["metrics"]
    rows = sorted(rows, key=lambda r: r["load_order"])
    drops = sorted({r["drop"] for r in rows})

    if g["replanned"]:
        html(banner(f"화주는 {g['planned_truck']} 기준으로 계획했지만, 내 차량({truck['name']}) 크기에 맞춰 "
                          f"같은 품목으로 다시 계산한 적재 순서입니다.", "amber"))
    if g["unplaced"]:
        html(banner(f"이 차량으로는 {len(g['unplaced'])}개를 실을 수 없습니다. 관제센터에 문의하세요.", "red"))
    html(stats([
        ("차량", truck["name"]),
        ("상자", f"{len(rows)}개 · {m['loaded_weight']:,.0f}kg"),
        ("하차지", f"{len(drops)}곳 · 부피 {m['volume_util'] * 100:.0f}%"),
    ]))
    st.caption("좌우 기준: 운전석에 앉아 앞을 볼 때 왼쪽 = 왼쪽 벽 · 거리는 mm · 운전석 쪽부터 실어서 뒷문 쪽으로 채웁니다.")
    if not rows:
        return

    load_tab, unload_tab = st.tabs(["상차 순서 · 적재 가이드", "하차 순서"])
    with load_tab:
        _loading(rows, truck, key, height)
    with unload_tab:
        _unloading(rows, truck, drops, key, height)


def _loading(rows, truck, key, height):
    n = len(rows)
    skey = f"{key}_step"
    st.session_state.setdefault(skey, 1)
    st.session_state[skey] = min(st.session_state[skey], n)
    b1, b2, b3 = st.columns([1, 4, 1], vertical_alignment="bottom")
    b1.button("◀ 이전", key=f"{key}_prev", width="stretch", on_click=_move, args=(skey, -1, n),
              disabled=st.session_state[skey] <= 1)
    step = b2.slider("적재 단계", 1, n, key=skey, label_visibility="collapsed")
    b3.button("다음 ▶", key=f"{key}_next", width="stretch", type="primary", on_click=_move, args=(skey, 1, n),
              disabled=step >= n)

    r = rows[step - 1]
    before = rows[:step - 1]
    left, right = st.columns([1, 1.6], gap="large")
    with left:
        lines = where_text(r, truck, before)
        rot = orientation_text(r)
        body = "".join(f'<div style="display:flex;gap:8px;padding:5px 0;font-size:14.5px">'
                       f'<span style="color:var(--brand);font-weight:800">{i}</span>'
                       f'<span class="ct-strong">{escape(t)}</span></div>' for i, t in enumerate(lines, start=1))
        if rot:
            body += banner(f"↻ {rot}", "amber")
        if not r["stackable"]:
            body += banner("이 상자 위에는 아무것도 올리지 마세요.", "red")
        html(f'<div class="ct-card">'
                f'<div class="ct-row"><span class="ct-eyebrow">STEP {step} / {n}</span>'
                f'<span class="ct-mono">{r["item_id"]}</span></div>'
                f'<div class="ct-big" style="margin-top:4px">{escape(r["name"])}</div>'
                f'<div class="ct-muted">{r["length"]:,} × {r["width"]:,} × {r["height"]:,}mm '
                f'(길이×폭×높이) · {r["weight_kg"]:,.0f}kg</div>'
                f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 6px">{_tags(r)}</div>'
                f'<div style="border-top:1px solid var(--line);margin-top:8px;padding-top:6px">{body}</div></div>')
        upcoming = rows[step:step + 4]
        if upcoming:
            section("다음 상자", f"남은 {n - step}개")
            html('<div class="ct-card" style="padding:10px 16px">' + "".join(
                f'<div class="ct-item ct-row"><span><span class="ct-mono">{q["load_order"]}</span> '
                f'<span class="ct-strong">{escape(q["name"])}</span></span>'
                f'<span class="ct-muted">{q["weight_kg"]:,.0f}kg · {q["drop"]}차</span></div>' for q in upcoming)
                + "</div>")
        else:
            html(banner("마지막 상자입니다. 적재 후 고정(래싱) 상태를 확인하고 출발하세요.", "green"))
    with right:
        fig = pack_3d.figure(truck, rows, upto=step, height=height, current_label="지금 실을 상자",
                             always_highlight=True)
        st.plotly_chart(fig, key=f"{key}_load3d", config={"displaylogo": False}, theme=None)


def _unloading(rows, truck, drops, key, height):
    if len(drops) == 1:
        st.caption("하차지가 1곳입니다. 실은 순서의 반대(뒷문 쪽 · 위쪽부터)로 내리면 됩니다.")
    stop = st.segmented_control("하차지", drops, format_func=lambda d: f"{d}차 하차지", default=drops[0],
                                key=f"{key}_stop") or drops[0]
    remaining = [r for r in rows if r["drop"] >= stop]          # 앞 하차지 화물은 이미 내림
    target = [r for r in remaining if r["drop"] == stop]
    faded = {r["item_id"] for r in remaining if r["drop"] != stop}
    out_order = sorted(target, key=lambda r: -r["load_order"])  # 나중에 실은 것부터 꺼냄
    left, right = st.columns([1, 1.6], gap="large")
    with left:
        w = sum(r["weight_kg"] for r in target)
        html(f'<div class="ct-card"><div class="ct-eyebrow">{stop}차 하차지</div>'
                f'<div class="ct-big">{len(target)}개 · {w:,.0f}kg 내리기</div>'
                f'<div class="ct-muted">남은 화물 {len(remaining) - len(target)}개는 그대로 둡니다 (회색)</div></div>')
        st.dataframe(pd.DataFrame([{
            "순서": n, "ID": r["item_id"], "품목": r["name"], "무게(kg)": r["weight_kg"],
            "위치": _door_text(r, truck),
        } for n, r in enumerate(out_order, start=1)]), hide_index=True, width="stretch",
            height=min(360, 38 + 35 * len(out_order)))
    with right:
        fig = pack_3d.figure(truck, remaining, color_by="drop", faded=faded, height=height)
        st.plotly_chart(fig, key=f"{key}_unload3d_{stop}", config={"displaylogo": False}, theme=None)
