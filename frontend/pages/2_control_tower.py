"""관제 타워 (Phase 7) · 통합관제 화면: 전국 관제 지도(자동 매칭 현황 포함) / 이상 감지 / 매칭 분석."""

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

import api
import ui

LAYERS = {"all": "전체", "transit": "운행중", "empty": "공차", "cold": "콜드체인", "anomaly": "이상감지"}
TYPE_LABEL = {"ALL": "전체 유형", "COLD": "온도 이상", "STAGNATION": "장시간 정체", "DEVIATION": "경로 이탈",
              "DELAY": "도착 지연", "SUSPICIOUS_REOUTSOURCING": "재위탁 의심"}


# ======================================================================= 전국 관제 지도
SEV = {"HIGH": ("긴급", "red"), "MEDIUM": ("주의", "amber"), "LOW": ("참고", "blue")}


def agent_panel():
    """AI 관제 브리핑: 지금 조치할 항목 · 근거 · 권장 조치(버튼) · 안내 문구 초안."""
    try:
        b = api.get("/admin/agent/briefing")
    except api.ApiError as e:
        st.error(str(e))
        return
    c = b["counts"]
    head = f"AI 관제 브리핑 · 긴급 {c['HIGH']} · 주의 {c['MEDIUM']} · 참고 {c['LOW']}"
    with st.expander(head, icon=":material/smart_toy:", expanded=c["HIGH"] > 0):
        st.caption(f"{ui.dt_text(b['generated_at'], '%H:%M:%S')} 기준")
        ui.html(f'<div class="ct-card"><b>{b["summary"]}</b></div>')
        for n, i in enumerate(b["insights"][:8]):
            label, tone = SEV[i["severity"]]
            with st.container(border=True):
                ev = " · ".join(e for e in i["evidence"] if e)
                ui.html(f'<div>{ui.pill(label, tone)} {ui.pill(i["category"], "gray")} '
                        f'<span class="ct-strong">{i["title"]}</span></div>'
                        f'<div style="margin-top:6px">{i["detail"]}</div>'
                        + (f'<div class="ct-muted" style="font-size:12.5px;margin-top:4px">근거: {ev}</div>' if ev else ""))
                a = i.get("action")
                cols = st.columns([1.3, 2.7], vertical_alignment="center")
                if a and a.get("endpoint"):
                    if cols[0].button(a["label"], key=f"agent_{n}_{a['type']}", width="stretch"):
                        body = {"amount": a["amount"]} if a["type"] == "incentive" else None
                        if ui.act(api.post, a["endpoint"], json=body, success=f"조치 완료: {a['label']}"):
                            st.rerun()
                elif a:
                    cols[0].caption(f"권장: {a['label']}")
                if i.get("message"):
                    with cols[1].popover("안내 문구 초안", icon=":material/chat:"):
                        st.code(i["message"], language=None, wrap_lines=True)
                        st.caption("복사해서 문자 · 메신저로 보내세요. (자동 발송하지 않습니다)")
        if len(b["insights"]) > 8:
            st.caption(f"외 {len(b['insights']) - 8}건")


def control_map():
    ui.page_header("CONTROL CENTER", "전국 관제 지도",
                   "차량 위치와 자동 매칭 진행 상황을 실시간으로 보고, 매칭에 실패한 화물을 조치하세요.")
    s = ui.load(api.get, "/admin/summary")
    ui.kpis([
        {"label": "네트워크 차량", "value": s["vehicles"], "unit": "대"},
        {"label": "운행 중", "value": s["in_transit"], "unit": "대",
         "note": f"콜드체인 {s['cold']}대 · GPS 수신 {s.get('gps_live', 0)}대 (가상 기사 · Redis Streams)"},
        {"label": "공차 대기", "value": s["empty"], "unit": "대", "note": "자동 매칭 후보"},
        {"label": "자동 매칭 진행", "value": s["matching"], "unit": "건", "note": f"기사 응답 대기 {s['offers_pending']}건"},
        {"label": "매칭 실패", "value": s["failed_matches"], "unit": "건", "alert": s["failed_matches"] > 0,
         "note": "제안할 기사 없음 → 조치 필요"},
        {"label": "이상 감지", "value": s["anomalies_open"], "unit": "건", "alert": s["anomalies_open"] > 0,
         "note": f"경로 이탈 {s.get('deviated', 0)} · 지연 {s.get('delayed', 0)} · 서지 권역 {s.get('surge_regions', 0)}"},
    ])

    agent_panel()
    tracking_market_panel()

    left, right = st.columns([2.1, 1], gap="large")
    with left:
        layer = st.segmented_control("레이어", list(LAYERS), format_func=LAYERS.get, default="all",
                                     label_visibility="collapsed", key="map_layer") or "all"
        live_map(layer)
        ui.html(ui.legend())
    with right:
        vehicle_panel()
        offers_list()

        st.write("")
        failed = ui.load(api.get, "/admin/failed-matches")
        ui.section(f"매칭 실패 · {len(failed)}", "제안할 수 있는 기사 없음")
        if not failed:
            ui.html('<div class="ct-card ct-muted">매칭에 실패한 화물이 없습니다.</div>')
        for f in failed:
            with st.container(border=True):
                ui.html(f'<div class="ct-row"><span class="ct-mono">{f["id"]}</span>'
                        f'{ui.pill("대기 " + ui.duration(f["wait_seconds"]), "amber")}</div>'
                        f'<div class="ct-strong" style="margin-top:4px">{f["title"]}</div>'
                        f'<div class="ct-muted">{f["origin"]} → {f["destination"]} · {ui.won(f["fare"])}</div>'
                        f'<div style="font-size:12.5px;color:#B54708;margin-top:4px">{" · ".join(f["reasons"])}</div>')
                if st.button("매칭 실패 조치", key=f"fm_{f['id']}", width="stretch"):
                    manual_dispatch_dialog(f)

        st.write("")
        open_anomalies = ui.load(api.get, "/admin/anomalies", status="UNSOLVED")
        ui.section(f"미해결 이상 감지 · {len(open_anomalies)}", "텔레매틱스 자동 감지")
        if not open_anomalies:
            ui.html('<div class="ct-card ct-muted">미해결 이상 건이 없습니다.</div>')
        for a in open_anomalies:
            with st.container(border=True):
                ui.html(f'<div class="ct-row">{ui.pill(a["type_label"], ui.STATUS_TONE[a["severity"]])}'
                        f'<span class="ct-muted" style="font-size:12px">{ui.ago(a["at"])}</span></div>'
                        f'<div class="ct-strong" style="margin-top:6px">{a["text"]}</div>'
                        f'<div class="ct-muted">{a["plate"]} · {a["driver"]} 차주 ({a["phone"]}) · {a["location"]}</div>')
                if st.button("조치 완료 처리", key=f"an_{a['id']}", icon=":material/task_alt:", width="stretch"):
                    if ui.act(api.post, f"/admin/anomalies/{a['id']}/resolve", success=f"{a['id']} 조치 완료 처리했습니다."):
                        st.rerun()


def tracking_market_panel():
    """실시간 추적(경로 이탈 디바운싱 · 지연 · 재위탁) + 권역 수급(D/S · 서지 · 날씨)."""
    with st.expander("실시간 추적 · 권역 수급", icon=":material/satellite_alt:"):
        t1, t2 = st.tabs(["GPS 추적 판정", "권역 수급 · 날씨"])
        with t1:
            tr = ui.load(api.get, "/admin/tracking")
            r = tr["rule"]
            st.caption(f"경로 거리 계산: {tr['engine']} · 기준 {r['deviation_km']:g}km 초과가 연속 {r['hits']}회면 경고 "
                       f"(GPS 튐 오탐 방지) · 지연 {r['delay_alert_min']}분 초과 알림 · 배정 기사가 아닌 단말 핑은 거절")
            if tr["runs"]:
                st.dataframe(pd.DataFrame([{
                    "화물": x["order_id"], "기사": f"{x['driver']} · {x['plate']}", "GPS": "수신 중" if x["live"] else "끊김",
                    "핑": x["pings"], "경로까지(km)": x["dist_km"], "연속 이탈": x["off_count"], "이탈 경고": x["alerts"],
                    "지연(분)": x["delay_minutes"], "재위탁 의심 핑": x["foreign_pings"],
                } for x in tr["runs"]]), hide_index=True, width="stretch", column_config={
                    "경로까지(km)": st.column_config.NumberColumn(format="%.2f"),
                    "연속 이탈": st.column_config.ProgressColumn(min_value=0, max_value=r["hits"], format="%d회")})
            else:
                st.info("운행 중인 차량이 없습니다.")
        with t2:
            mk = ui.load(api.get, "/pricing/market")
            st.caption("수요 = 매칭 대기 화물 + 시간대별 포아송 가상 콜 · 공급 = 빈 기사 + 권역 기본 공차 · "
                       "D/S 1.5 초과 시 서지(+10%~+30%) · 날씨는 확률 분포 모의(비 +15% · 폭설 +30%)")
            st.dataframe(pd.DataFrame([{
                "권역": m["region"], "수요": m["demand"], "공급": m["supply"], "D/S": m["ds_ratio"],
                "서지": f"+{m['surge']:.0%}" if m["surge"] else "-", "날씨": m["weather"]["label"],
                "러시아워": "●" if m["rush_hour"] else "",
            } for m in mk]), hide_index=True, width="stretch", column_config={
                "D/S": st.column_config.ProgressColumn(min_value=0, max_value=4, format="%.2f")})


# ======================================================================= 우측 상세 패널
def _pick_vehicle(key: str):
    st.session_state.ct_vehicle = st.session_state[key]
    st.session_state.ct_vehicle_closed = None


def _close_vehicle():
    st.session_state.ct_vehicle_closed = st.session_state.get("ct_vehicle")
    st.session_state.ct_vehicle = None


def vehicle_panel():
    """오른쪽 상세 패널: 지도에서 차량을 누르거나 목록에서 고르면 기사 연락처 · 현재 운행 · 감사 로그 ·
    인수증 · 리뷰를 한 번에 보여 준다."""
    fleet = ui.load(api.get, "/admin/fleet")
    named = [v for v in fleet if v["named"]]
    ids = [v["id"] for v in named]
    label = {v["id"]: f"{v['plate']} · {v['driver']} · {v['status_text'][:14]}" for v in named}
    cur = st.session_state.get("ct_vehicle")
    key = f"ct_pick_{cur}"
    # 목록을 직접 바꿨을 때만 반영 (지도에서 누른 배경 차량은 목록에 없어도 패널이 닫히지 않게)
    st.selectbox("차량 상세", [None, *ids], index=(ids.index(cur) + 1) if cur in ids else 0,
                 format_func=lambda i: "지도에서 차량을 누르거나 여기서 선택" if i is None else label[i],
                 key=key, label_visibility="collapsed", on_change=_pick_vehicle, args=(key,))
    if not cur:
        return
    if not cur.startswith("D-"):
        with st.container(border=True):
            st.caption("배경 차량(시연용 네트워크 차량)은 등록 기사가 아니라 상세 정보가 없습니다.")
            st.button("닫기", on_click=_close_vehicle, key="ct_close_bg")
        return
    d = ui.load(api.get, f"/admin/drivers/{cur}/detail")
    dr, r = d["driver"], d["rating"]
    plate_ok = dr["plate_type"] == "COMMERCIAL_YELLOW"
    with st.container(border=True):
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.html(f'<div class="ct"><div class="ct-eyebrow">VEHICLE · {dr["id"]}</div>'
                f'<div class="ct-big">{dr["name"]} 차주 <span style="color:#F79009;font-size:15px">'
                f'{ui.stars(r["avg"], r["count"])}</span></div></div>')
        c2.button("닫기", on_click=_close_vehicle, key="ct_close", icon=":material/close:", width="stretch")
        badges = (ui.pill(d["state"], "blue") + " "
                  + ui.pill("노란 번호판" if plate_ok else "번호판 부적합", "green" if plate_ok else "red")
                  + (" " + ui.pill("이용 제한", "red") if dr["blacklisted"] else ""))
        ui.html(f'<div>{badges}</div>'
                f'<div class="ct-muted" style="margin-top:6px">📞 <b class="ct-strong">{dr["phone"]}</b>'
                f'{" · " + ui.escape(dr["email"]) if dr.get("email") else ""}</div>'
                f'<div class="ct-muted">{dr["plate"]} · {dr["truck_type"]} ({dr["tonnage"]:g}톤) · 대기 {dr["location"]} · '
                f'이번 달 {dr["month_runs"]}회 · 완료 {d["completed"]}건</div>'
                + (f'<div style="color:#B42318;font-size:12.5px">제한 사유: {ui.escape((dr["blacklist"] or {}).get("reason", ""))}</div>'
                   if dr["blacklisted"] else ""))

        run = d["run"]
        if run:
            ui.section("현재 운행", run["id"])
            gps = (f'GPS {"수신 중" if run["gps_live"] else "끊김"} · 핑 {run["pings"]}회 · 경로까지 '
                   f'{run["dist_km"] if run["dist_km"] is not None else "-"}km · 연속 이탈 {run["off_count"]}회')
            ui.html(f'<div class="ct-strong">{ui.escape(run["title"])}</div>'
                    f'<div class="ct-muted">{run["shipper"]} · {ui.won(run["fare"])}</div>'
                    + ui.route_bar(run["origin"], run["destination"], run["progress"],
                                   f"진행 {run['progress']:.0f}% · {run['speed']}km/h",
                                   f"도착 {run['eta_clock'] or '-'}")
                    + f'<div class="ct-muted" style="font-size:12.5px">{gps}</div>'
                    + f'<div class="ct-muted" style="font-size:12.5px">계획 경로(planned_polyline) {run["planned_points"]}개 지점 · '
                      f'{run["polyline_chars"]}자</div>'
                    + (ui.banner(f"지연 +{run['delay_minutes']}분", "amber") if run["delay_minutes"] else "")
                    + (ui.banner("계획 경로 이탈 중", "red") if run["deviated"] else ""))
        elif d["offer"]:
            o = d["offer"]
            ui.section("배차 제안 응답 대기", o["id"])
            ui.html(f'<div class="ct-muted">{ui.escape(o["title"])} · {o["origin"]} → {o["destination"]}</div>')
        for a in d["anomalies"]:
            ui.html(ui.banner(f"⚠ {a['type_label']} · {a['text']}", "red"))

        t1, t2, t3 = st.tabs([f"감사 로그 · {len(d['audit'])}", "인수증", f"리뷰 · {r['count']}"])
        with t1:
            if d["audit"]:
                st.caption(f"{d['audit_order']} · SHA-256 해시 체인 (최근 12줄)")
                ui.html("".join(
                    f'<div style="font-size:12.5px;padding:3px 0;border-top:1px solid var(--line)">'
                    f'<span class="ct-mono">{ui.dt_text(e["at"], "%m.%d %H:%M")}</span> <b>{ui.escape(e["label"])}</b> '
                    f'<span class="ct-muted">{e["from"] or ""}→{e["to"]} · {ui.escape(e["actor"])} · '
                    f'{(e.get("current_hash") or "")[:10]}</span></div>' for e in d["audit"]))
            else:
                st.caption("감사 로그가 없습니다.")
        with t2:
            pod = d["pod"]
            if pod:
                ui.html(f'<div class="ct-strong">{pod["order_id"]} · {ui.escape(pod["title"])}</div>'
                        f'<div class="ct-muted">인수자 {ui.escape(pod["receiver"])} · 서명 {ui.dt_text(pod["signed_at"], "%Y.%m.%d %H:%M")}</div>'
                        f'<div class="ct-mono" style="font-size:12.5px">해시 {pod["hash"]}</div>')
            else:
                st.caption("발급된 전자인수증이 없습니다.")
        with t3:
            if d["reviews"]:
                if r["top_tags"]:
                    ui.html('<div>' + " ".join(ui.pill(t, "gray") for t in r["top_tags"]) + '</div>')
                ui.html("".join(
                    f'<div style="font-size:12.5px;padding:4px 0;border-top:1px solid var(--line)">'
                    f'<span style="color:#F79009">{ui.star_bar(v["stars"])}</span> '
                    f'<span class="ct-muted">{ui.escape(v["writer"])} · {ui.dt_text(v["at"], "%m.%d")} · '
                    f'{ui.escape(", ".join(v["tags"]))}</span>'
                    + (f'<div>{ui.escape(v["comment"])}</div>' if v["comment"] else "") + '</div>'
                    for v in d["reviews"]))
            else:
                st.caption("받은 리뷰가 없습니다.")
    st.write("")


@st.fragment(run_every="3s")
def offers_list():
    try:
        offers = api.get("/admin/offers")
    except api.ApiError as e:
        st.error(str(e))
        return
    ui.section(f"기사 응답 대기 · {len(offers)}", "자동 매칭이 보낸 배차 제안")
    if not offers:
        ui.html('<div class="ct-card ct-muted">응답을 기다리는 제안이 없습니다.</div>')
        return
    rows = ""
    for o in offers:
        m = o["matching"]
        util = f"{m['util'] * 100:.0f}%" if m.get("util") is not None else "-"
        left = m["expires_in"]
        rows += (f'<div class="ct-item"><div class="ct-row"><span class="ct-mono">{o["id"]}</span>'
                 f'{ui.pill(f"{left}초 남음", "red" if left <= 15 else "blue")}</div>'
                 f'<div class="ct-strong" style="margin-top:4px">{o["title"]}</div>'
                 f'<div class="ct-muted">→ {m["driver"]["name"]} 기사 ({ui.stars(m["driver"].get("rating"), None, "평가 없음")}) · '
                 f'{m["driver"]["truck_type"]} · 상차 {m["pickup_km"]:.0f}km'
                 f' · 적재율 {util} · 점수 {m["score"] * 100:.0f}</div></div>')
    ui.html(f'<div class="ct-card" style="padding:12px 16px">{rows}</div>')


@st.fragment(run_every="3s")
def live_map(layer: str):
    try:
        fleet = api.get("/admin/fleet")
    except api.ApiError as e:
        st.error(str(e))
        return
    df = pd.DataFrame(fleet)
    if layer == "transit":
        df = df[df["kind"].isin(["transit", "cold"])]
    elif layer != "all":
        df = df[df["kind"] == layer]
    df = df.assign(color=df["kind"].map(ui.MAP_COLORS),
                   speed_text=df["speed"].map(lambda v: f"{v} km/h" if v else "정차"),
                   temp_text=df["temp"].map(lambda t: "" if pd.isna(t) else f" · {t:+.1f}°C"))
    hubs = pd.DataFrame(_hubs(), columns=["name", "lat", "lon"])
    # 등록 기사의 운행 경로 (상차지 → 하차지 도로 경로)
    routes = df[df["path"].notna()][["path", "kind"]].copy() if "path" in df else pd.DataFrame()
    if not routes.empty:
        routes["line"] = routes["kind"].map(lambda k: ui.MAP_COLORS[k] + [110])
    df = df.drop(columns=["path"], errors="ignore")  # 점 레이어에는 경로가 필요 없음 (전송량 절약)
    layers = [
        pdk.Layer("PathLayer", data=routes, get_path="path", get_color="line", width_min_pixels=2.5,
                  joint_rounded=True, cap_rounded=True),
        pdk.Layer("ScatterplotLayer", data=hubs, get_position="[lon, lat]", get_fill_color=[79, 70, 229, 35],
                  get_radius=14000, radius_min_pixels=10),
        pdk.Layer("ScatterplotLayer", data=df[df["kind"] == "anomaly"], get_position="[lon, lat]",
                  get_fill_color=[226, 55, 43, 60], get_radius=16000, radius_min_pixels=16),
        # 등록 차주(이름 있는 차량)는 바깥 링으로 강조 — 번호판·상태는 마우스 툴팁으로 확인
        pdk.Layer("ScatterplotLayer", data=df[df["named"]], get_position="[lon, lat]", filled=False, stroked=True,
                  get_line_color=[14, 17, 22], get_radius=9000, radius_min_pixels=10, line_width_min_pixels=2),
        pdk.Layer("ScatterplotLayer", data=df, id="trucks", get_position="[lon, lat]", get_fill_color="color",
                  get_radius=4500, radius_min_pixels=5, stroked=True, get_line_color=[255, 255, 255],
                  line_width_min_pixels=1.5, pickable=True),
    ]
    deck = pdk.Deck(
        layers=layers, map_style=pdk.map_styles.CARTO_LIGHT,
        initial_view_state=pdk.ViewState(latitude=36.25, longitude=127.75, zoom=6.3),
        tooltip={"html": "<b>{plate}</b> · {truck_type}<br/>{driver} · {status_text}<br/>{speed_text}{temp_text}<br/>{cargo}",
                 "style": {"backgroundColor": "#0E1116", "color": "#F7F8FA", "fontSize": "12px",
                           "borderRadius": "8px", "padding": "8px 10px"}},
    )
    event = st.pydeck_chart(deck, height=560, key="fleet_map", on_select="rerun", selection_mode="single-object")
    # 차량 점 클릭 → 오른쪽 상세 패널 열기 (패널은 지도 밖에 있으므로 화면 전체를 다시 그린다)
    picked = ((getattr(event, "selection", None) or {}).get("objects") or {}).get("trucks") or []
    vid = picked[0].get("id") if picked else None
    if vid and vid != st.session_state.get("ct_vehicle") and vid != st.session_state.get("ct_vehicle_closed"):
        st.session_state.ct_vehicle = vid
        st.session_state.ct_vehicle_closed = None
        st.rerun()


@st.cache_data(ttl=600, show_spinner=False)
def _hubs() -> list[dict]:
    """물류 거점 (백엔드 spatial.HUBS 와 같은 목록 · /meta)."""
    try:
        return [{k: h[k] for k in ("name", "lat", "lon")} for h in api.get("/meta").get("hubs", [])]
    except api.ApiError:
        return []


@st.dialog("매칭 실패 조치", width="large")
def manual_dispatch_dialog(f: dict):
    ui.html(f'<div class="ct-row"><span class="ct-mono">{f["id"]}</span>'
            f'{ui.pill("대기 " + ui.duration(f["wait_seconds"]), "amber")}</div>'
            f'<div class="ct-big">{f["title"]}</div>'
            f'<div class="ct-muted">{f["origin"]} → {f["destination"]} · 요청 {f["truck_type"]}</div>'
            + ui.stats([("희망 운임", ui.won(f["fare"])), ("AI 추천 운임", ui.won(f["recommended"])),
                        ("차이", ui.won(f["recommended"] - f["fare"]))])
            + ui.banner("매칭 실패 요인 · " + " / ".join(f["reasons"]), "amber"))

    ui.section("① 관제 보조금 지급 후 재공고", "운임을 올리고 거절 이력을 지운 뒤 자동 매칭을 다시 돌립니다")
    c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
    amount = c1.select_slider("보조금", options=[10_000, 30_000, 50_000, 80_000, 100_000], value=30_000,
                              format_func=lambda v: f"+{v:,}원 (총 {f['fare'] + v:,}원)")
    if c2.button("인상 후 재공고", type="primary", width="stretch"):
        if ui.act(api.post, f"/admin/failed-matches/{f['id']}/incentive", {"amount": amount},
                  success=f"보조금 +{amount:,}원 적용 — 자동 매칭을 다시 시작했습니다."):
            st.rerun()

    ui.section("② 재매칭", "운임은 그대로 두고 거절 이력만 지운 뒤 다시 매칭")
    if st.button("거절 이력 초기화 후 재매칭", icon=":material/refresh:", width="stretch"):
        if ui.act(api.post, f"/admin/failed-matches/{f['id']}/rematch", success="자동 매칭을 다시 시작했습니다."):
            st.rerun()

    ui.section("③ 기사 직접 지정", "이 화물이 전량 실리고 지금 비어 있는 기사 (반경 · 거절 이력 무관, 가까운 순)")
    cands = ui.load(api.get, f"/admin/failed-matches/{f['id']}/candidates")
    if not cands:
        st.info("지금 지정할 수 있는 기사가 없습니다. (적재 가능한 차량이 모두 운행 중이거나 제안 대기 중)")
    for c in cands:
        a, b = st.columns([3, 1], vertical_alignment="center")
        flags = []
        if not c["in_radius"]:
            flags.append(f"반경 {c['pickup_km']:.0f}km 밖")
        if c["declined"]:
            flags.append("이 화물 거절함")
        util = f"{c['util'] * 100:.0f}%" if c["util"] is not None else "-"
        a.html(f'<div class="ct"><div class="ct-strong">{c["name"]} 기사 · {c["plate"]}</div>'
               f'<div class="ct-muted">{c["truck_type"]} · {c["location"]} (상차 {c["pickup_km"]:.0f}km) · 적재율 {util}'
               f' · {c["fit_note"]}{" · " + " · ".join(flags) if flags else ""}</div></div>')
        if b.button("직접 지정", key=f"assign_{c['id']}", width="stretch"):
            if ui.act(api.post, f"/admin/failed-matches/{f['id']}/assign", {"driver_id": c["id"]},
                      success=f"{c['name']} 기사에게 직접 배차했습니다."):
                st.rerun()


# ======================================================================= 이상 감지
def anomalies():
    ui.page_header("CONTROL CENTER", "이상 감지 로그", "온도 이탈 · 장시간 정체 · 경로 이탈 감지 목록입니다. 확인 후 조치 완료로 닫으세요.")
    s = ui.load(api.get, "/admin/summary")
    open_rows = ui.load(api.get, "/admin/anomalies", status="UNSOLVED")
    ui.kpis([
        {"label": "미해결 이상", "value": s["anomalies_open"], "unit": "건", "alert": s["anomalies_open"] > 0},
        {"label": "평균 대응 시간", "value": ui.duration(s["avg_response_seconds"]), "note": "감지 → 조치 완료"},
        {"label": "온도 이탈 (미해결)", "value": sum(1 for r in open_rows if r["type"] == "COLD"), "unit": "건"},
        {"label": "경로 이탈 · 정체 (미해결)", "value": sum(1 for r in open_rows if r["type"] != "COLD"), "unit": "건"},
    ])
    f1, f2, _ = st.columns([1, 1, 3])
    type_ = f1.selectbox("유형", list(TYPE_LABEL), format_func=TYPE_LABEL.get, label_visibility="collapsed")
    status = f2.selectbox("상태", ["ALL", "UNSOLVED", "RESOLVED"], label_visibility="collapsed",
                          format_func={"ALL": "전체 상태", "UNSOLVED": "미해결", "RESOLVED": "조치완료"}.get)
    rows = ui.load(api.get, "/admin/anomalies", type=type_, status=status)
    ui.section("감지 이력", f"{len(rows)}건")
    if not rows:
        st.info("조건에 맞는 이상 감지 로그가 없습니다.")
        return
    df = pd.DataFrame([{
        "로그 ID": r["id"], "시각": ui.dt_text(r["at"], "%m.%d %H:%M"), "차량": f"{r['plate']} · {r['driver']}",
        "유형": r["type_label"], "내용": r["text"], "위치": r["location"], "심각도": r["severity"],
        "상태": "미해결" if r["status"] == "UNSOLVED" else "조치완료",
    } for r in rows])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="anomaly_table", column_config={
                             "로그 ID": st.column_config.TextColumn(width=130),
                             "시각": st.column_config.TextColumn(width=85),
                             "차량": st.column_config.TextColumn(width=150),
                             "유형": st.column_config.TextColumn(width=80),
                             "내용": st.column_config.TextColumn(width=200),
                             "위치": st.column_config.TextColumn(width=190),
                             "심각도": st.column_config.TextColumn(width=65),
                             "상태": st.column_config.TextColumn(width=70)})
    sel = event.selection.rows
    if not sel or sel[0] >= len(rows):
        st.caption("행을 선택하면 상세 확인 · 조치 완료 처리를 할 수 있습니다.")
        return
    r = rows[sel[0]]
    temp = (f' · 현재 {ui.temp_text(r["temp_now"])} / 설정 {ui.temp_text(r["temp_set"])}'
            if r["type"] == "COLD" and r["temp_now"] is not None else "")
    ui.html(f'<div class="ct-card"><div class="ct-row"><span class="ct-mono">{r["id"]} · {ui.dt_text(r["at"], "%H:%M:%S")}</span>'
            f'{ui.pill(r["severity"], ui.STATUS_TONE[r["severity"]])}</div>'
            f'<div class="ct-big" style="margin-top:4px">{r["text"]}</div>'
            f'<div class="ct-muted">{r["plate"]} · {r["driver"]} 차주 ({r["phone"]}) · {r["location"]}</div>'
            f'<div class="ct-muted">{r["detail"]}{temp}</div></div>')
    b1, b2, _ = st.columns([1, 1, 2])
    if r["status"] == "UNSOLVED":
        if b1.button("조치 완료 처리", type="primary", icon=":material/task_alt:", width="stretch"):
            if ui.act(api.post, f"/admin/anomalies/{r['id']}/resolve", success=f"{r['id']} 조치 완료 처리했습니다."):
                st.rerun()
    if b2.button("AI 인시던트 리포트", icon=":material/summarize:", width="stretch"):
        rep = ui.load(api.get, f"/admin/agent/incident/{r['id']}")
        st.caption(f"{rep.get('engine', 'rule-based')}" + (f" · {rep['fallback']}" if rep.get("fallback") else ""))
        st.code(rep["text"], language=None, wrap_lines=True)


# ======================================================================= 매칭 분석
def settings_panel(cfg: dict):
    """자동 매칭 설정: 저장하면 바로 매칭 대기 화물(매칭 실패 포함)에 새 기준으로 다시 매칭한다."""
    d = cfg["defaults"]
    changed = any(cfg[k] != d[k] for k in d)
    with st.expander("자동 매칭 설정 · " + ("기본값과 다름 (변경됨)" if changed else "기본값 사용 중"),
                     icon=":material/tune:", expanded=False):
        with st.form("match_settings", border=False):
            c1, c2, c3 = st.columns(3, gap="large")
            radius = c1.slider("탐색 반경 (상차지까지 도로거리)", 10, 200, int(cfg["radius_km"]), 5, format="%dkm",
                               help="이 거리 안의 공차에게만 자동 제안. 넓히면 매칭 실패는 줄고 평균 상차 거리는 늘어납니다.")
            timeout = c2.slider("기사 수락 제한시간", 15, 300, int(cfg["timeout_sec"]), 5, format="%d초",
                                help="이 시간 안에 응답이 없으면 다음 순위 기사에게 넘어갑니다. 새로 보내는 제안부터 적용.")
            w = c3.slider("점수 가중치 · 거리", 0, 100, round(cfg["w_dist"] * 100), 5, format="%d%%",
                          help="나머지는 적재율 가중치. 거리를 높이면 가까운 차량, 낮추면 적재함을 꽉 채우는 차량이 우선.")
            c3.caption(f"→ 점수 = 거리 {w}% + 적재율 {100 - w}%")
            b1, b2, _ = st.columns([1, 1, 2])
            save = b1.form_submit_button("저장하고 바로 적용", type="primary", width="stretch", icon=":material/check:")
            reset = b2.form_submit_button("기본값으로", width="stretch", icon=":material/restart_alt:")
        if save or reset:
            body = ({"radius_km": d["radius_km"], "timeout_sec": d["timeout_sec"], "w_dist": d["w_dist"]} if reset
                    else {"radius_km": radius, "timeout_sec": timeout, "w_dist": w / 100})
            if all(body[k] == cfg[k] for k in body):
                st.info("바뀐 설정이 없습니다.")
                return
            res = ui.act(api.put, "/admin/matching/settings", body)
            if res:
                last = res["log"][0] if res["log"] else None
                ui.flash("매칭 설정을 적용했습니다." + (f" ({last['note']})" if last else ""))
                st.rerun()
        if cfg["log"]:
            ui.html('<div class="ct-muted" style="font-size:12.5px;margin-top:6px"><b>변경 이력</b>' + "".join(
                f'<div style="margin-top:3px">{ui.dt_text(r["at"], "%m.%d %H:%M")} · {ui.escape(r["by"])} · '
                f'{ui.escape(" / ".join(r["changes"]))} · {ui.escape(r["note"])}</div>' for r in cfg["log"]) + "</div>")
        else:
            st.caption("아직 변경 이력이 없습니다. 서버를 다시 켜면 기본값(반경 50km · 60초 · 거리 60%)으로 돌아갑니다.")


def engine_panel(e: dict | None):
    """3단계 하이브리드 매칭 엔진: PostGIS 필터 → 규칙 + LightGBM 점수 → 헝가리안 배정."""
    if not e:
        return
    ml, lr = e["ml"], e.get("last_round")
    with st.expander("3단계 매칭 엔진 · " + e["pipeline"], icon=":material/hub:"):
        c1, c2, c3 = st.columns(3, gap="medium")
        with c1:
            ui.html('<div class="ct-card"><div class="ct-muted" style="font-size:12px">① 공간 필터</div>'
                    f'<div class="ct-strong" style="font-size:16px;margin:4px 0">{e["spatial"]}</div>'
                    '<div class="ct-muted" style="font-size:12.5px">기사 대기 위치(PostGIS geography)가 상차지 반경 '
                    '안인 기사만 다음 단계로 · 그다음 도로거리로 다시 확인</div></div>')
        with c2:
            auc = f"AUC {ml['auc']:.3f}" if ml.get("auc") else "-"
            ui.html('<div class="ct-card"><div class="ct-muted" style="font-size:12px">② 스코어링 · '
                    f'규칙 {100 - e["ml_weight"] * 100:.0f}% + AI {e["ml_weight"] * 100:.0f}%</div>'
                    f'<div class="ct-strong" style="font-size:16px;margin:4px 0">{ml["model"]} · {auc}</div>'
                    f'<div class="ct-muted" style="font-size:12.5px">⚠ <b>{ml["data"]}</b>로 학습 '
                    f'({ml.get("samples", 0):,}건, 검증 {ml.get("test", 0):,}건). 실운행 기록이 쌓이면 같은 피처로 재학습 · '
                    f'<b>lgb_score ≥ {e.get("ml_threshold", 0.75):.2f}</b> 인 후보만 자동 제안</div></div>')
        with c3:
            if lr:
                gain = lr["total"] - lr["greedy_total"]
                body = (f'대기 화물 {lr["orders"]}건 × 후보 기사 {lr["drivers"]}명 → {lr["assigned"]}건 배정 · '
                        f'점수 합 {lr["total"]:.2f} (탐욕 방식 {lr["greedy_total"]:.2f}, '
                        f'{"+" if gain >= 0 else ""}{gain:.2f})')
            else:
                body = "아직 배정 라운드 기록이 없습니다."
            ui.html('<div class="ct-card"><div class="ct-muted" style="font-size:12px">③ 최적 배정</div>'
                    '<div class="ct-strong" style="font-size:16px;margin:4px 0">헝가리안 알고리즘</div>'
                    f'<div class="ct-muted" style="font-size:12.5px">최근 라운드 · {body}</div></div>')
        imp = ml.get("importance") or {}
        if imp:
            label = {"pickup_km": "상차 거리", "util": "적재율", "fare_gap": "운임 vs 적정가", "trip_km": "운송 거리",
                     "cold": "콜드체인", "urgent": "긴급", "accept_rate": "기사 수락률", "cf_score": "권역 선호(cf)",
                     "rating": "기사 평점", "rush_hour": "러시아워"}
            total = sum(imp.values()) or 1
            df = pd.DataFrame([{"피처": label.get(k, k), "중요도": v / total} for k, v in imp.items()])
            chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4, color=ui.BRAND).encode(
                x=alt.X("중요도:Q", axis=alt.Axis(format="%"), title="LightGBM 피처 중요도 (gain · 가상 데이터)"),
                y=alt.Y("피처:N", sort="-x", title=None),
                tooltip=[alt.Tooltip("피처:N"), alt.Tooltip("중요도:Q", format=".1%")])
            st.altair_chart(chart.properties(height=200), width="stretch")


def metrics_panel(m: dict | None):
    """실시간 매칭 메트릭스 (matching_metrics_logs) + AI 임계값 · 가중치 조절 가이드."""
    if not m:
        return
    with st.expander(f"매칭 성능 로그 · 배정 라운드 {m['rounds']}회", icon=":material/speed:"):
        p95 = m["dispatch_latency_p95_sec"]
        ui.kpis([
            {"label": "배차 지연 p95", "value": "-" if p95 is None else f"{p95:.2f}", "unit": "초",
             "alert": p95 is not None and p95 >= m["target_p95_sec"], "note": f"목표 < {m['target_p95_sec']:.0f}초"},
            {"label": "공차 거리 단축률", "value": "-" if m.get("deadhead_vs_nearest_pct") is None
             else f"{m['deadhead_vs_nearest_pct']:.1f}", "unit": "%",
             "alert": m.get("deadhead_vs_nearest_pct") is not None
             and m["deadhead_vs_nearest_pct"] < m.get("target_deadhead_vs_nearest_pct", 10),
             "note": f"단순 최단거리 매칭 대비 · 목표 ≥ {m.get('target_deadhead_vs_nearest_pct', 10):.0f}%"
                     + f" · 비교 라운드 {m.get('comparable_rounds', 0)}회 (후보 1명뿐인 라운드 제외)"
                     + ("" if m["empty_deadhead_km_reduction_pct"] is None
                        else f" · 점수순 그리디 대비 {m['empty_deadhead_km_reduction_pct']:.1f}%")},
            {"label": "거절률", "value": "-" if m["rejection_rate_pct"] is None else f"{m['rejection_rate_pct']:.1f}",
             "unit": "%", "alert": m["rejection_rate_pct"] is not None and m["rejection_rate_pct"] >= 20,
             "note": "거절 + 무응답 / 응답 · 목표 < 15~20%"},
            {"label": "임계값 차단", "value": m["threshold_filtered"], "unit": "쌍",
             "note": f"lgb_score < {m['ml_threshold']:.2f} → 자동 제안 안 함"},
        ])
        if m["recent"]:
            st.dataframe(pd.DataFrame([{
                "라운드": r["id"], "시각": ui.dt_text(r["at"], "%H:%M:%S"), "화물": r["orders"], "후보 기사": r["drivers"],
                "배정": r["assigned"], "지연(ms)": r["latency_ms"], "헝가리안 상차(km)": r["hungarian_avg_km"],
                "최단거리 기준선(km)": r.get("nearest_avg_km"), "단축률 vs 최단(%)": r.get("deadhead_vs_nearest_pct"),
                "그리디 상차(km)": r["greedy_avg_km"], "단축률 vs 그리디(%)": r["deadhead_reduction_pct"],
                "임계값 차단": r["threshold_filtered"], "실패": r["failed"],
            } for r in m["recent"]]), hide_index=True, width="stretch")
        g = ui.load(api.get, "/admin/agent/tuning")
        ui.html('<div class="ct-muted" style="font-size:12.5px;margin-top:6px"><b>AI 조절 가이드</b> (자동 적용하지 않음)'
                + "".join(f'<div style="margin-top:3px">• {ui.escape(t["target"])}: {ui.escape(str(t["now"]))} → '
                          f'<b>{ui.escape(str(t["suggest"]))}</b> — {ui.escape(t["why"])}</div>' for t in g["tips"])
                + "</div>")


def matching():
    d = ui.load(api.get, "/admin/matching")
    k, cfg = d["kpi"], d["settings"]
    ui.page_header("CONTROL CENTER", "자동 매칭 분석",
                   f"점수 = {cfg['formula']} · 탐색 반경 {cfg['radius_km']}km · "
                   f"수락 제한 {cfg['timeout_sec']}초 · 모든 값은 매칭 기록에서 계산")
    settings_panel(cfg)
    engine_panel(d.get("engine"))
    metrics_panel(d.get("metrics"))

    def pct(v, digits=1):
        return "-" if v is None else f"{v:.{digits}f}"

    ui.kpis([
        {"label": "매칭 성공률", "value": pct(k["match_success_rate"]), "unit": "%", "note": "배차 확정 / (확정 + 현재 실패)"},
        {"label": "자동 매칭 비율", "value": pct(k["auto_rate"]), "unit": "%", "note": "관제 직접 지정 제외"},
        {"label": "평균 배차 소요", "value": ui.duration(k["avg_match_seconds"]),
         "note": "등록 → 배차 확정 · 기사 응답 " + ui.duration(k.get("avg_response_seconds"))
                 + " + 시스템 매칭 " + ("-" if k.get("system_match_seconds") is None
                                         else f"{k['system_match_seconds']:.2f}초 (실측)")},
        {"label": "평균 상차 거리", "value": pct(k["avg_pickup_km"]), "unit": "km", "note": "배차 당시 기사 위치 기준"},
        {"label": "평균 유효 적재율", "value": pct(k["avg_util"] * 100 if k.get("avg_util") else None, 0),
         "unit": "%", "note": "부피 · 중량 중 큰 쪽 · 부피 "
                              + pct(k["avg_volume_util"] * 100 if k["avg_volume_util"] else None, 0) + "% · 중량 "
                              + pct(k["avg_weight_util"] * 100 if k.get("avg_weight_util") else None, 0) + "%"},
        {"label": "제안 수락률", "value": pct(k["accept_rate"]), "unit": "%", "note": "수락 / (수락 + 거절 + 무응답)"},
    ])

    c1, c2 = st.columns([1.3, 1], gap="large")
    with c1:
        ui.section("상차 거리 분포", "배차 확정 건 · 배차 당시 기사 위치 → 상차지")
        dist = pd.DataFrame(d["pickup_dist"])
        bar = alt.Chart(dist).mark_bar(cornerRadiusEnd=6, color=ui.BRAND).encode(
            x=alt.X("range:N", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("count:Q", title="건수", axis=alt.Axis(tickMinStep=1)),
            tooltip=[alt.Tooltip("range:N", title="거리"), alt.Tooltip("count:Q", title="건수")])
        labels = bar.mark_text(dy=-8, color=ui.MUTED).encode(text="count:Q")
        st.altair_chart((bar + labels).properties(height=280), width="stretch")
    with c2:
        ui.section("차종별 배정 건수", "어떤 차량에 배차됐는지")
        mix = pd.DataFrame(d["truck_mix"])
        if mix.empty:
            st.info("아직 배정 기록이 없습니다.")
        else:
            bar = alt.Chart(mix).mark_bar(cornerRadiusEnd=6, color=ui.INK).encode(
                x=alt.X("count:Q", title="건수", axis=alt.Axis(tickMinStep=1)),
                y=alt.Y("truck_type:N", sort="-x", title=None),
                tooltip=[alt.Tooltip("truck_type:N", title="차종"), alt.Tooltip("count:Q", title="건수")])
            labels = bar.mark_text(align="left", dx=6, color=ui.MUTED).encode(text="count:Q")
            st.altair_chart((bar + labels).properties(height=280), width="stretch")

    ui.section("최근 배차 확정", "최근 10건")
    if d["recent"]:
        st.dataframe(pd.DataFrame([{
            "화물 ID": r["id"], "화물": r["title"], "기사": r["driver"], "차종": r["truck_type"],
            "방식": r["by"], "상차 거리": r["pickup_km"], "유효 적재율": r.get("util"), "부피 적재율": r["volume_util"],
            "점수": round(r["score"] * 100) if r["score"] is not None else None, "제안 횟수": r["offers"],
            "배차 소요": ui.duration(r["match_seconds"]),
        } for r in d["recent"]]), hide_index=True, width="stretch", column_config={
            "상차 거리": st.column_config.NumberColumn(format="%.1fkm"),
            "유효 적재율": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1,
                                                       help="부피 · 중량 중 큰 쪽"),
            "부피 적재율": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
            "점수": st.column_config.NumberColumn(format="%d점"),
            "제안 횟수": st.column_config.NumberColumn(format="%d회"),
        })

    ui.section("기사별 매칭 현황", "배정 건수 많은 순")
    st.dataframe(pd.DataFrame([{
        "기사": r["name"], "차량": r["plate"], "차종": r["truck_type"],
        "현재": "이용 제한" if r.get("blacklisted") else r["state"], "배정 건수": r["matched"],
        "평점": r.get("rating"), "리뷰": r.get("reviews"),
        "평균 상차 거리": r["avg_pickup_km"], "받은 제안": r["offers"], "수락률": r["accept_rate"],
    } for r in d["drivers"]]), hide_index=True, width="stretch", column_config={
        "평점": st.column_config.NumberColumn(format="★%.1f", help="화주 리뷰 평균 · 매칭 AI 피처"),
        "리뷰": st.column_config.NumberColumn(format="%d건"),
        "배정 건수": st.column_config.NumberColumn(format="%d건"),
        "평균 상차 거리": st.column_config.NumberColumn(format="%.1fkm"),
        "받은 제안": st.column_config.NumberColumn(format="%d건"),
        "수락률": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
    })
