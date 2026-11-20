"""
적재 계획 3D 시각화 (Plotly).

좌표계는 백엔드와 동일: x = 길이(0=운전석, 커질수록 뒷문), y = 폭, z = 높이(바닥 0), 단위 mm, 상자 모서리(최솟값) 기준.
- 상자: 품목(SKU)별로 Mesh3d 하나에 모아서 그린다 (상자 수가 많아도 가볍게)
- 테두리: 모든 상자의 12개 모서리를 Scatter3d 선 하나로
- EP: 해당 단계에서 후보였던 Extreme Point 들을 점으로, 실제로 고른 EP 는 크게 표시
"""

import plotly.graph_objects as go

PALETTE = ["#14B8A6", "#F59E0B", "#3B82F6", "#EC4899", "#22C55E", "#F97316", "#64748B", "#06B6D4", "#EAB308"]
BRAND = "#4F46E5"
VIOLATION = "#EF4444"
INK = "#0E1116"

# 정육면체 8개 꼭짓점 → 삼각형 12개 / 모서리 12개
_TRI = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (3, 2, 6), (3, 6, 7), (0, 3, 7), (0, 7, 4), (1, 2, 6), (1, 6, 5)]
_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
GAP = 6  # 상자끼리 붙어 보이지 않도록 화면에서만 살짝 안쪽으로 줄임 (mm)


def _corners(x, y, z, dx, dy, dz, inset=0):
    x0, y0, z0 = x + inset, y + inset, z + inset
    x1, y1, z1 = x + dx - inset, y + dy - inset, z + dz - inset
    return [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]


def _hover(r: dict) -> str:
    sup = f"{r['support_ratio'] * 100:.0f}%" if r.get("support_ratio") is not None else "-"
    load = f"{r['load_on_top']:,.0f}kg" if r.get("load_on_top") is not None else "-"
    cap = "적층 불가" if not r["stackable"] else ("무제한" if r["max_top_load"] is None else f"{r['max_top_load']:,.0f}kg")
    flags = " · ".join(f for f, on in (("세움 고정", r["upright"]), ("회전됨", r["rotated"])) if on)
    return (f"<b>{r['name']}</b> ({r['item_id']})<br>"
            f"{r['length']}×{r['width']}×{r['height']}mm · {r['weight_kg']:,.0f}kg {('· ' + flags) if flags else ''}<br>"
            f"위치 ({r['x']}, {r['y']}, {r['z']}) · 적재 {r['load_order']}번째 · {r['drop']}차 하차<br>"
            f"지지 {sup} · 상부 하중 {load} / 허용 {cap}")


def _mesh(rows: list[dict], color: str, name: str, opacity: float = 1.0, showlegend=True) -> go.Mesh3d:
    xs, ys, zs, ii, jj, kk, texts = [], [], [], [], [], [], []
    for n, r in enumerate(rows):
        for cx, cy, cz in _corners(r["x"], r["y"], r["z"], r["length"], r["width"], r["height"], GAP):
            xs.append(cx), ys.append(cy), zs.append(cz)
            texts.append(_hover(r))
        base = n * 8
        for a, b, c in _TRI:
            ii.append(base + a), jj.append(base + b), kk.append(base + c)
    return go.Mesh3d(x=xs, y=ys, z=zs, i=ii, j=jj, k=kk, color=color, opacity=opacity, flatshading=True,
                     name=name, showlegend=showlegend, hovertext=texts, hoverinfo="text",
                     lighting=dict(ambient=0.55, diffuse=0.85, specular=0.05, roughness=0.9),
                     lightposition=dict(x=2000, y=-3000, z=6000))


def _edges(rows: list[dict], color=INK, width=2, name="edges") -> go.Scatter3d:
    xs, ys, zs = [], [], []
    for r in rows:
        pts = _corners(r["x"], r["y"], r["z"], r["length"], r["width"], r["height"], GAP)
        for a, b in _EDGES:
            xs += [pts[a][0], pts[b][0], None]
            ys += [pts[a][1], pts[b][1], None]
            zs += [pts[a][2], pts[b][2], None]
    return go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(color=color, width=width),
                        hoverinfo="skip", showlegend=False, name=name)


def _container(truck: dict) -> list:
    L, W, H = truck["length"], truck["width"], truck["height"]
    pts = _corners(0, 0, 0, L, W, H)
    xs, ys, zs = [], [], []
    for a, b in _EDGES:
        xs += [pts[a][0], pts[b][0], None]
        ys += [pts[a][1], pts[b][1], None]
        zs += [pts[a][2], pts[b][2], None]
    frame = go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", line=dict(color="#98A2B3", width=3, dash="dash"),
                         hoverinfo="skip", showlegend=False)
    floor = go.Mesh3d(x=[0, L, L, 0], y=[0, 0, W, W], z=[0, 0, 0, 0], i=[0, 0], j=[1, 2], k=[2, 3],
                      color="#E4E7EC", opacity=0.55, hoverinfo="skip", showlegend=False)
    labels = go.Scatter3d(x=[0, L], y=[W / 2, W / 2], z=[H + H * 0.08, H + H * 0.08], mode="text",
                          text=["◀ 운전석", "뒷문 ▶"], textfont=dict(size=13, color=INK),
                          hoverinfo="skip", showlegend=False)
    return [floor, frame, labels]


DROP_PALETTE = ["#14B8A6", "#F59E0B", "#EC4899", "#3B82F6", "#22C55E", "#F97316", "#64748B", "#06B6D4", "#EAB308"]
FADED = "#D0D5DD"


def figure(truck: dict, rows: list[dict], *, upto: int | None = None, eps: list | None = None,
           chosen_ep: list | None = None, violations: set[str] | None = None, height: int = 600,
           color_by: str = "sku", faded: set[str] | None = None, current_label: str = "방금 놓은 상자",
           always_highlight: bool = False) -> go.Figure:
    """rows 는 적재 순서대로. upto 개까지만 그리고, 마지막(upto번째) 상자를 강조한다.
    color_by='drop' 이면 하차지별 색, faded 에 든 상자는 회색 반투명(배경)으로 그린다."""
    violations = violations or set()
    faded = faded or set()
    n = len(rows) if upto is None else max(0, min(upto, len(rows)))
    shown = rows[:n]
    # 마지막 단계에선 강조 안 함 (always_highlight=True 면 마지막 상자도 강조 — 적재 가이드용)
    current = shown[-1] if (upto is not None and n > 0 and (n < len(rows) or always_highlight)) else None

    traces = _container(truck)
    groups: dict[int, list[dict]] = {}
    dim = [r for r in shown if r["item_id"] in faded and r is not current]
    for r in shown:
        if r is current or r["item_id"] in violations or r["item_id"] in faded:
            continue
        groups.setdefault(r["drop"] if color_by == "drop" else r["sku"], []).append(r)
    for key, rs in sorted(groups.items()):
        if color_by == "drop":
            traces.append(_mesh(rs, DROP_PALETTE[(key - 1) % len(DROP_PALETTE)], f"{key}차 하차 ({len(rs)})"))
        else:
            traces.append(_mesh(rs, PALETTE[key % len(PALETTE)], rs[0]["name"]))
    if dim:
        traces.append(_mesh(dim, FADED, f"그 외 ({len(dim)})", opacity=0.22))
    bad = [r for r in shown if r["item_id"] in violations and r is not current]
    if bad:
        traces.append(_mesh(bad, VIOLATION, f"제약 위반 ({len(bad)})"))
    if current:
        traces.append(_mesh([current], BRAND, f"{current_label} · {current['item_id']}"))
    traces.append(_edges([r for r in shown if r["item_id"] not in faded or r is current]))
    if dim:
        traces.append(_edges(dim, color="#C3C8D1", width=1))

    if eps:
        traces.append(go.Scatter3d(
            x=[p[0] for p in eps], y=[p[1] for p in eps], z=[p[2] for p in eps], mode="markers",
            marker=dict(size=3.5, color=BRAND, symbol="diamond", line=dict(color="#fff", width=0.5)),
            name=f"후보 EP ({len(eps)})", hovertemplate="EP (%{x}, %{y}, %{z})<extra></extra>"))
    if chosen_ep:
        traces.append(go.Scatter3d(
            x=[chosen_ep[0]], y=[chosen_ep[1]], z=[chosen_ep[2]], mode="markers",
            marker=dict(size=9, color=INK, symbol="diamond", line=dict(color=BRAND, width=3)),
            name="선택된 EP", hovertemplate="선택된 EP (%{x}, %{y}, %{z})<extra></extra>"))

    axis = dict(showbackground=False, gridcolor="#E4E7EC", zerolinecolor="#D0D5DD", color="#667085",
                tickfont=dict(size=10))
    fig = go.Figure(traces)
    fig.update_layout(
        height=height, margin=dict(l=0, r=0, t=10, b=0), paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Pretendard, 'Malgun Gothic', sans-serif", color=INK),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=11), bgcolor="rgba(0,0,0,0)"),
        uirevision=truck["name"],  # 단계를 바꿔도 카메라 각도 유지
        scene=dict(
            aspectmode="data", uirevision=truck["name"],
            xaxis=dict(title="길이 x (mm)", **axis), yaxis=dict(title="폭 y", **axis),
            zaxis=dict(title="높이 z", **axis),
            camera=dict(eye=dict(x=0.95, y=-1.9, z=1.15), center=dict(x=0.05, y=0, z=-0.15),
                        up=dict(x=0, y=0, z=1)),
        ),
        hoverlabel=dict(bgcolor=INK, font=dict(color="#F7F8FA", size=12)),
    )
    return fig
