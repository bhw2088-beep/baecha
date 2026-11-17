"""
화주 포털 (Phase 1)
  - 배차 · 실시간 관제 : dispatch()
  - 신규 화물 등록     : new_order_page()  ① 운송 정보 → ② 적재 품목 → ③ 3D 적재 시뮬레이션 → ④ AI 운임 · 등록
  - 정산 · 운송 이력   : history()

등록 시 품목 목록을 함께 보내고, 서버가 같은 엔진으로 다시 계산해 전량 적재 가능할 때만 저장한다.
"""

import hashlib
from datetime import datetime, time, timedelta
import json
import math

import pandas as pd
import streamlit as st

import api
import nav
import packviz
import reviews_ui
import ui


# ================================================================== 배차 · 실시간 관제 / 정산 · 운송 이력

LOG_TONE = {"offered": "brand", "accepted": "green", "assigned": "green", "declined": "gray", "expired": "amber",
            "reposted": "blue", "rematch": "blue", "cancelled": "gray"}


def _status_text(o: dict) -> str:
    if o["anomaly"]:
        return "⚠ 이상감지"
    m = o.get("matching")
    if m:
        return {"OFFERED": "기사 응답 대기", "FAILED": "매칭 실패"}.get(m["state"], "후보 탐색 중")
    return o["status_label"]


# ======================================================================= 배차 · 실시간 관제
def dispatch():
    user = st.session_state.user
    head, btn = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        ui.page_header(f"SHIPPER · {user['sub']}", "배차 · 실시간 관제",
                       "화물을 등록하면 3D 적재로 다 실리는 차량 중 상차지에서 가까운 기사에게 자동으로 배차를 제안합니다.")
    with btn:
        if st.button("신규 화물 등록", icon=":material/add:", type="primary", width="stretch"):
            st.switch_page(nav.PAGES["new_order"])

    summary = ui.load(api.get, "/shipper/summary")
    orders = ui.load(api.get, "/shipper/orders")

    count = lambda s: sum(1 for o in orders if o["status"] == s)  # noqa: E731
    steps = [
        ("STEP 01", "화물 등록 · 3D 적재", "품목 입력 → 적재 시뮬레이션", len(orders)),
        ("STEP 02", "자동 매칭", "적재 가능 · 가까운 기사에게 제안", count("MATCHING")),
        ("STEP 03", "배차 완료", "기사 수락 · 상차지 이동", count("ASSIGNED")),
        ("STEP 04", "운송 중", "GPS 트래킹 · 전자인수증", count("IN_TRANSIT")),
    ]
    ui.html('<div class="ct-pipe">' + "".join(
        f'<div class="ct-step"><div class="no">{no}</div><div class="c">{c}</div>'
        f'<div class="t">{t}</div><div class="d">{d}</div></div>' for no, t, d, c in steps) + "</div>")

    ui.kpis([
        {"label": "최근 30일 등록 화물", "value": summary["orders_30d"], "unit": "건"},
        {"label": "운행 · 배송 중", "value": summary["active"], "unit": "건", "note": "실시간 GPS 트래킹"},
        {"label": "자동 매칭 진행", "value": summary["matching"], "unit": "건",
         "note": f"응답 대기 {summary['offered']} · 실패 {summary['match_failed']}", "alert": summary["match_failed"] > 0},
        {"label": "평균 배차 소요", "value": ui.duration(summary["avg_match_seconds"]), "note": "등록 → 기사 수락"},
        {"label": "평균 부피 적재율", "value": f"{summary['avg_volume_util'] * 100:.0f}" if summary["avg_volume_util"] else "-",
         "unit": "%", "note": "배정 차량 기준"},
        {"label": "이상 감지", "value": summary["anomalies"], "unit": "건", "alert": summary["anomalies"] > 0,
         "note": "관제센터 대응 중" if summary["anomalies"] else "정상 운행"},
    ])

    if not orders:
        ui.section("진행 중인 화물")
        st.info("진행 중인 화물이 없습니다. 신규 화물을 등록해 보세요.")
        return
    n_run = sum(1 for o in orders if o["status"] in ("ASSIGNED", "IN_TRANSIT"))
    ui.section(f"운행 중 차량 지도 · {n_run}대", "도로 경로 · 파랑 운행 중 · 청록 콜드체인 · 빨강 이상 감지 · 회색 상차 대기")
    fleet_map_panel()
    st.write("")
    ui.section("진행 중인 화물", "행을 선택하면 아래에 실시간 상세가 열립니다")
    df = pd.DataFrame([{
        "화물 ID": o["id"],
        "화물": o["title"],
        "구간": f"{_short(o['origin'])} → {_short(o['destination'])}",
        "차주": (f"{o['driver']['name']} · {o['driver']['plate']}" if o["driver"]
               else f"(제안) {o['matching']['driver']['name']}" if o["matching"] and o["matching"]["state"] == "OFFERED"
               else "매칭 대기"),
        "기사 평점": ui.stars((o["driver"] or (o["matching"] or {}).get("driver") or {}).get("rating"),
                          (o["driver"] or (o["matching"] or {}).get("driver") or {}).get("reviews"), "-"),
        "운임": o["fare"],
        "상태": _status_text(o),
        "진행률": o["progress"] / 100,
    } for o in orders])
    event = st.dataframe(
        df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key=f"shipper_orders_table_{len(orders)}", height=38 + 35 * len(df),
        column_config={
            "화물 ID": st.column_config.TextColumn(width=125),
            "화물": st.column_config.TextColumn(width=175),
            "구간": st.column_config.TextColumn(width=185),
            "차주": st.column_config.TextColumn(width=160),
            "운임": st.column_config.NumberColumn(format="%,d원", width=90),
            "상태": st.column_config.TextColumn(width=100),
            "진행률": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1, width=110),
        },
    )
    rows = event.selection.rows
    selected_id = orders[rows[0]]["id"] if rows and rows[0] < len(orders) else orders[0]["id"]
    st.write("")
    ui.section("실시간 상세", "4초마다 자동 갱신")
    order_live_panel(selected_id)


@st.fragment(run_every="5s")
def fleet_map_panel():
    try:
        orders = api.get("/shipper/orders")
    except api.ApiError as e:
        st.error(str(e))
        return
    ui.fleet_routes_map(orders, height=480, key="shipper_fleet_map")


def _short(addr: str) -> str:
    parts = addr.split()
    return " ".join(parts[:2]) if len(parts) > 1 else addr


@st.fragment(run_every="4s")
def order_live_panel(order_id: str):
    try:
        o = api.get(f"/orders/{order_id}")
    except api.ApiError as e:
        st.error(str(e))
        return
    if o["status"] == "MATCHING":
        render_order_detail(o)
        return
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        render_order_detail(o, show_map=False)
    with right:
        ui.route_map(o, height=330)


def render_order_detail(o: dict, show_map: bool = True):
    d = o["driver"]
    top = (f'<div class="ct-row"><span class="ct-mono">{o["id"]}</span>{ui.status_pill(o)}</div>'
           f'<div class="ct-big" style="margin-top:6px">{o["title"]}</div>'
           f'<div class="ct-muted">{o["truck_type"]} · {o["distance_km"]:.0f}km · 운임 {ui.won(o["fare"])}'
           + (f' · 예약 상차 {ui.dt_text(o["pickup_at"], "%m.%d %H:%M")}' if o.get("pickup_at") else "") + '</div>')
    if o.get("packing"):
        pk = o["packing"]
        top += (f'<div class="ct-muted" style="margin-top:2px">적재 계획 · 상자 {pk["placed"]}개 · '
                f'부피 {pk["volume_util"] * 100:.0f}% · 중량 {pk["weight_util"] * 100:.0f}% ({pk["loaded_weight"]:,.0f}kg)</div>')
    if o["status"] == "MATCHING":
        body = ui.route_bar(o["origin"], o["destination"], 0, f"대기 {ui.duration(o['wait_seconds'])}",
                            f"운송 {o['distance_km']:.0f}km")
        body += matching_block(o)
    else:
        remain = o["distance_km"] * (1 - o["progress"] / 100)
        body = ui.route_bar(o["origin"], o["destination"], o["progress"],
                            f"진행률 {o['progress']:.0f}%", f"남은 거리 {remain:.0f}km")
        eta = f"{o['eta_clock']}" if o["eta_clock"] else "-"
        body += ui.stats([
            ("현재 속도", f"{o['speed']} km/h" if o["status"] == "IN_TRANSIT" else "상차 대기"),
            ("도착 예정", eta),
            ("적재 온도", (ui.temp_text(o["temp_now"]) if o["temp_now"] is not None else f"설정 {ui.temp_text(o['temp_set'])}")
             if o["cold_chain"] else "상온"),
        ])
        if o["anomaly"]:
            body += ui.banner(f"⚠ {o['anomaly']['text']} — 관제센터가 대응 중입니다.", "red")
        elif o["delay_minutes"]:
            body += ui.banner(f"도착 예정 +{o['delay_minutes']}분 지연", "amber")
    driver = ""
    if d:
        mt = o.get("match")
        how = ""
        if mt:
            util = f" · 부피 적재율 {mt['volume_util'] * 100:.0f}%" if mt["volume_util"] is not None else ""
            score = f" · 점수 {mt['score'] * 100:.0f}" if mt["score"] is not None else ""
            by = mt["by_label"]
            offers = f" · 제안 {mt['offers']}회" if mt["offers"] else ""
            how = (f'<div class="ct-muted" style="font-size:12px">{by} · 배차 당시 상차지까지 {mt["pickup_km"]:.0f}km'
                   f'{util}{score}{offers}</div>')
        driver = (f'<div class="ct-item" style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px">'
                  f'<div class="ct-row"><div><div class="ct-strong">{d["name"]} 차주 '
                  f'<span style="color:#F79009">{ui.stars(d.get("rating"), d.get("reviews"))}</span></div>'
                  f'<div class="ct-muted">{d["plate"]} · {d["truck_type"]}</div>{how}</div>'
                  f'<div class="ct-strong">{d["phone"]}</div></div></div>')
    ui.html(f'<div class="ct-card">{top}{body}{driver}</div>')
    if show_map and o["status"] != "MATCHING":
        ui.route_map(o, height=220)


def matching_block(o: dict) -> str:
    """자동 매칭 진행 상황 (화주 · 관제 공용 HTML)."""
    m = o["matching"]
    if m["state"] == "OFFERED":
        dv = m["driver"]
        util = f"{m['util'] * 100:.0f}%" if m.get("util") is not None else "-"
        html = ui.banner(f"자동 매칭 · {dv['name']} 기사 ({dv['plate']} · {dv['truck_type']} · "
                         f"{ui.stars(dv.get('rating'), dv.get('reviews'))})에게 배차 제안 중 — "
                         f"응답 남은 시간 {m['expires_in']}초", "blue")
        html += ui.stats([("상차지까지", f"{m['pickup_km']:.0f}km"), ("기사 차량 적재율", util),
                          ("매칭 점수", f"{m['score'] * 100:.0f}점")])
        ml = (f' · AI 수락 예측 {m["ml_prob"] * 100:.0f}% (가상 데이터 기반)' if m.get("ml_prob") is not None else "")
        html += (f'<div class="ct-muted" style="font-size:12px;margin-top:6px">헝가리안 최적 배정 · 후보 {m["candidates"]}대 중 '
                 f'{m.get("rank") or 1}순위{ml} · {m["offer_round"]}번째 제안 · 거절하거나 {m["timeout"]}초 안에 '
                 '응답이 없으면 다시 배정합니다</div>')
    elif m["state"] == "FAILED":
        html = ui.banner("매칭 실패 · 지금 제안할 수 있는 기사가 없습니다 — 관제센터가 확인 중입니다", "red")
        html += '<div style="margin-top:4px">' + "".join(
            f'<div class="ct-muted" style="font-size:12.5px">· {r}</div>' for r in m["reasons"]) + "</div>"
        html += ('<div class="ct-muted" style="font-size:12px;margin-top:6px">조건에 맞는 공차가 생기면 자동으로 다시 제안합니다.</div>')
    else:
        html = ui.banner(m.get("wait_note") or "후보 기사를 찾는 중입니다", "amber")
    log = o.get("match_log") or []
    if log:
        html += '<div style="margin-top:10px">' + "".join(
            f'<div class="ct-row" style="padding:4px 0;font-size:12.5px"><span>{ui.pill(e["label"], LOG_TONE.get(e["event"], "gray"))}'
            f' <span class="ct-muted">{e["driver"] or ""} {e["note"]}</span></span>'
            f'<span class="ct-mono">{ui.dt_text(e["at"], "%H:%M:%S")}</span></div>' for e in log[-5:][::-1]) + "</div>"
    return html


# ======================================================================= 정산 · 운송 이력
def history():
    user = st.session_state.user
    ui.page_header(f"SHIPPER · {user['sub']}", "정산 · 운송 이력",
                   "완료된 운송의 전자인수증을 확인하고 청구서를 승인하세요.")
    summary = ui.load(api.get, "/shipper/summary")
    ui.kpis([
        {"label": "최근 30일 정산 완료", "value": ui.won(summary["settled_30d"])},
        {"label": "미정산 (운송완료)", "value": ui.won(summary["unpaid"]),
         "note": f"청구서 승인 대기 {summary['invoices_waiting']}건", "alert": summary["invoices_waiting"] > 0},
        {"label": "발급 전자인수증", "value": summary["pod_count"], "unit": "건", "note": "해시 검증 완료"},
        {"label": "운송 완료", "value": summary["done_count"], "unit": "건", "note": "운송완료 + 정산완료"},
    ])
    invoice_section()
    reviews_ui.pending_reviews_panel("shipper")

    f1, f2, f3, f4 = st.columns([2.2, 1, 1, 1], vertical_alignment="bottom")
    q = f1.text_input("검색", placeholder="화물명, 화물 ID, 차주명", label_visibility="collapsed")
    status = f2.selectbox("상태", ["ALL", "SETTLED", "DELIVERED"], label_visibility="collapsed",
                          format_func={"ALL": "전체 상태", "SETTLED": "정산완료", "DELIVERED": "운송완료 (정산대기)"}.get)
    data = ui.load(api.get, "/shipper/history", q=q, status=status, period=st.session_state.get("hist_period", "ALL"))
    period = f3.selectbox("기간", ["ALL", *data["periods"]], key="hist_period", label_visibility="collapsed",
                          format_func=lambda p: "전체 기간" if p == "ALL" else f"{p[:4]}년 {p[5:]}월")
    try:
        xlsx = api.get_bytes("/shipper/history/export", q=q, status=status, period=period)
        f4.download_button("엑셀 다운로드", xlsx, file_name="baecha_history.xlsx", icon=":material/download:",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    except api.ApiError as e:
        f4.error(str(e))

    items = data["items"]
    ui.section("운송 이력", f"총 {len(items)}건")
    if not items:
        st.info("조건에 맞는 운송 이력이 없습니다.")
        return
    df = pd.DataFrame([{
        "화물 ID": i["id"], "완료일": ui.dt_text(i["delivered_at"], "%Y.%m.%d"), "화물": i["title"],
        "구간": f"{_short(i['origin'])} → {_short(i['destination'])}",
        "차주": f"{i['driver']['name']} · {i['driver']['plate']}" if i["driver"] else "-",
        "최종 운임": i["total_fee"], "상태": i["status_label"], "청구": i["billing_label"],
        "내 평가": ui.star_bar((i.get("review") or {}).get("by_shipper")),
        "상차 거리": i["match"]["pickup_km"] if i["match"] else None,
    } for i in items])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="history_table", column_config={
        "화물 ID": st.column_config.TextColumn(width=130), "완료일": st.column_config.TextColumn(width=85),
        "화물": st.column_config.TextColumn(width=200), "구간": st.column_config.TextColumn(width=190),
        "차주": st.column_config.TextColumn(width=150), "최종 운임": st.column_config.NumberColumn(format="%,d원", width=95),
        "상태": st.column_config.TextColumn(width=70), "청구": st.column_config.TextColumn(width=70),
        "내 평가": st.column_config.TextColumn(width=85, help="이 운송에 내가 남긴 기사 별점 · 위 '상호 평가'에서 등록"),
        "상차 거리": st.column_config.NumberColumn(format="%.0fkm", width=75,
                                                 help="배차 당시 기사 위치 → 상차지 거리 (자동 매칭 기록)")})
    rows = event.selection.rows
    if not rows or rows[0] >= len(items):
        st.caption("행을 선택하면 전자인수증 확인 · 청구서 승인을 할 수 있습니다.")
        return
    item = items[rows[0]]
    _history_actions(item)


def _history_actions(i: dict):
    d = i["driver"] or {}
    fees = (f'기본 {i["fare"]:,} + 추가 {i["extra_fee"]:,} + 통행료 {i["toll_fee"]:,}')
    ui.html(f'<div class="ct-card"><div class="ct-row"><div><span class="ct-mono">{i["id"]}</span>'
            f'<div class="ct-big">{i["title"]}</div><div class="ct-muted">{d.get("name", "-")} 차주 · '
            f'{d.get("plate", "")} · 완료 {ui.dt_text(i["delivered_at"], "%Y.%m.%d %H:%M")}</div></div>'
            f'<div style="text-align:right"><div class="ct-big">{ui.won(i["total_fee"])}</div>'
            f'<div class="ct-muted">{fees}</div></div></div>'
            + (f'<div class="ct-muted" style="margin-top:8px">차주 메모: {i["memo"]}</div>' if i["memo"] else "")
            + "</div>")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("전자인수증", icon=":material/description:", width="stretch"):
        pod_dialog(i)
    if i["billing"] == "REQUESTED":
        if c2.button("청구서 승인 · 결제", icon=":material/check_circle:", type="primary", width="stretch"):
            pay_dialog(i)
    else:
        c2.button(i["billing_label"], disabled=True, width="stretch")
    p = i.get("payment")
    if p:
        state = "승인" if p["status"] == "APPROVED" else f"취소 ({p['cancel_reason']})"
        st.caption(f"모의 PG 결제 · {p['method_label']} · 거래번호 {p['tid']} · 승인번호 {p['approval_no']} · "
                   f"{ui.won(p['amount'])} · {state}")


PAY_METHODS = {"corp_card": "법인카드 (모의)", "transfer": "계좌이체 (모의)", "virtual": "가상계좌 (모의)"}


@st.dialog("청구서 승인 · 결제 (모의 PG)")
def pay_dialog(i: dict):
    st.write(f"**{i['id']} · {i['title']}**")
    st.write(f"결제 금액 **{ui.won(i['total_fee'])}** (VAT 포함 · 기본 {i['fare']:,} + 추가 {i['extra_fee']:,} "
             f"+ 통행료 {i['toll_fee']:,})")
    method = st.radio("결제 수단", list(PAY_METHODS), format_func=PAY_METHODS.get, horizontal=True)
    st.info("모의 결제입니다. 실제 카드 · 계좌 정보를 받지 않고, 실제 출금도 일어나지 않습니다. "
            "승인하면 모의 거래번호가 발급되고 정산 완료로 바뀝니다.", icon=":material/info:")
    c1, c2 = st.columns(2)
    if c1.button("취소", width="stretch"):
        st.rerun()
    if c2.button("결제 승인", type="primary", width="stretch"):
        try:
            r = api.post(f"/shipper/history/{i['id']}/approve-invoice", json={"method": method})
        except api.ApiError as e:
            st.error(str(e))
            return
        ui.flash(f"{i['id']} 결제 승인 · 거래번호 {r['payment']['tid']} · 정산 완료")
        st.rerun()


def invoice_section():
    """월별 인보이스: 그 달에 결제 승인된 운송비 (공급가액 · 부가세 · 합계)."""
    try:
        inv = api.get("/shipper/invoices", period=st.session_state.get("inv_period"))
    except api.ApiError as e:
        st.error(str(e))
        return
    with st.expander(f"월별 인보이스 · {inv['period'][:4]}년 {inv['period'][5:]}월 · {ui.won(inv['total'])} "
                     f"({inv['count']}건, 모의 PG)", icon=":material/request_quote:"):
        if inv["periods"]:
            st.selectbox("정산월", inv["periods"], key="inv_period",
                         format_func=lambda p: f"{p[:4]}년 {p[5:]}월")
        ui.html(f'<div class="ct-card"><div class="ct-row"><div><div class="ct-muted">인보이스 번호</div>'
                f'<div class="ct-strong">{inv["invoice_no"]}</div><div class="ct-muted">{inv["shipper"]} · '
                f'사업자 {inv["biz_no"]}</div></div><div style="text-align:right"><div class="ct-muted">'
                f'공급가액 {ui.won(inv["supply"])} · 부가세 {ui.won(inv["vat"])}</div>'
                f'<div class="ct-big">{ui.won(inv["total"])}</div></div></div></div>')
        if inv["lines"]:
            st.dataframe(pd.DataFrame([{
                "화물 ID": ln["order_id"], "화물": ln["title"], "차주": ln["driver"],
                "결제일": ln["approved_at"][:10], "결제 수단": ln["method_label"], "거래번호": ln["tid"],
                "공급가액": ln["supply"], "부가세": ln["vat"], "합계": ln["amount"],
            } for ln in inv["lines"]]), hide_index=True, width="stretch", column_config={
                "공급가액": st.column_config.NumberColumn(format="%,d원"),
                "부가세": st.column_config.NumberColumn(format="%,d원"),
                "합계": st.column_config.NumberColumn(format="%,d원")})
        else:
            st.caption("이 달에 결제 승인된 운송비가 없습니다.")


@st.dialog("전자 화물인수증 (e-POD)")
def pod_dialog(i: dict):
    pod, d = i["pod"], i["driver"] or {}
    rows = [("화물 ID", i["id"]), ("화물명", i["title"]), ("상차지", i["origin"]), ("하차지", i["destination"]),
            ("운송 차주", f"{d.get('name', '-')} ({d.get('plate', '-')})"), ("운임", ui.won(i["total_fee"])),
            ("완료 일시", ui.dt_text(pod["signed_at"], "%Y.%m.%d %H:%M"))]
    table = "".join(f'<div class="ct-row" style="padding:6px 0;border-top:1px solid var(--line)">'
                    f'<span class="ct-muted">{k}</span><span class="ct-strong">{v}</span></div>' for k, v in rows)
    ui.html(f'<div class="ct-card">{ui.pill("위변조 검증 완료 " + pod["hash"], "green")}'
            f'<div style="margin-top:12px">{table}</div>'
            f'<div style="margin-top:14px;padding:14px;border:1px dashed var(--line);border-radius:10px;text-align:center">'
            f'<div class="ct-muted">인수자 확인 및 전자서명</div>'
            f'<div style="font-size:24px;font-weight:800;margin-top:4px;font-style:italic">{pod["receiver"]} (인)</div></div></div>')
    ph = pod.get("photo") or {}
    if ph.get("sha256"):
        st.caption(f"인수증 사진 SHA-256 {ph['sha256'][:16]}… · {ph.get('filename') or '-'}"
                   + (" (시드 데이터 · 사진 파일 없음)" if ph.get("seed") else ""))
        if not ph.get("seed"):
            try:
                st.image(api.get_bytes(f"/orders/{i['id']}/pod/photo"), caption="인수증 사진", width="stretch")
            except api.ApiError as e:
                st.caption(str(e))
    elif ph.get("legacy"):
        st.caption("사진 인증 도입 전에 발급된 인수증입니다.")


# ================================================================== 신규 화물 등록 + 3D 적재 시뮬레이션

COLS = {  # 화면 컬럼명 → API 필드
    "품목": "name", "길이(mm)": "length", "폭(mm)": "width", "높이(mm)": "height", "무게(kg)": "weight",
    "수량": "qty", "세움 고정": "upright", "적층 불가": "stackable", "파손주의": "fragile", "냉동": "frozen",
    "상부 허용하중(kg)": "max_top_load", "하차 순서": "drop",
}
MERIT = {"wall": "벽 쌓기 (운전석→뒷문)", "floor": "바닥 우선 (낮게)"}
SORT = {"area_height": "면적-높이 군집 (Crainic)", "volume": "부피 내림차순", "weight": "무게 내림차순"}
STATUS_ICON = {"pass": ("✓", "green"), "warn": ("!", "amber"), "fail": ("✕", "red")}


def _meta():
    if "_pack_meta" not in st.session_state:
        st.session_state._pack_meta = ui.load(api.get, "/packing/meta")
    if "_meta" not in st.session_state:
        st.session_state._meta = ui.load(api.get, "/meta")
    return st.session_state._pack_meta, st.session_state._meta


def _regions(meta: dict) -> list[str]:
    """상차지 · 하차지 드롭다운 목록 (서버의 지역 목록 · 도로 경로 캐시에 있는 지역만)."""
    if not meta.get("regions"):
        st.session_state._meta = meta = ui.load(api.get, "/meta")
    return meta["regions"]


def _items_df(items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "품목": i["name"], "길이(mm)": i["length"], "폭(mm)": i["width"], "높이(mm)": i["height"],
        "무게(kg)": float(i["weight"]), "수량": i["qty"], "세움 고정": i["upright"], "적층 불가": not i["stackable"],
        "파손주의": bool(i.get("fragile")), "냉동": bool(i.get("frozen")),
        "상부 허용하중(kg)": i["max_top_load"], "하차 순서": i["drop"],
    } for i in items], columns=list(COLS))


def _df_to_items(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for n, row in enumerate(df.to_dict("records"), start=1):
        if all((v is None or (isinstance(v, float) and math.isnan(v)) or v == "") for k, v in row.items()
               if k not in ("세움 고정", "적층 불가", "파손주의", "냉동")):
            continue  # 빈 줄
        missing = [k for k in ("품목", "길이(mm)", "폭(mm)", "높이(mm)", "무게(kg)", "수량")
                   if row.get(k) is None or (isinstance(row[k], float) and math.isnan(row[k])) or row[k] == ""]
        if missing:
            errors.append(f"{n}번째 줄: {', '.join(missing)} 입력 필요")
            continue
        cap = row.get("상부 허용하중(kg)")
        cap = None if cap is None or (isinstance(cap, float) and math.isnan(cap)) else float(cap)
        drop = row.get("하차 순서")
        items.append({
            "name": str(row["품목"]), "length": int(row["길이(mm)"]), "width": int(row["폭(mm)"]),
            "height": int(row["높이(mm)"]), "weight": float(row["무게(kg)"]), "qty": int(row["수량"]),
            "upright": bool(row.get("세움 고정")), "stackable": not bool(row.get("적층 불가")),
            "fragile": bool(row.get("파손주의")), "frozen": bool(row.get("냉동")),
            "max_top_load": cap, "drop": 1 if drop is None or (isinstance(drop, float) and math.isnan(drop)) else int(drop),
        })
    return items, errors


def _hash(obj) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ======================================================================= page
def new_order_page():
    user = st.session_state.user
    pack_meta, meta = _meta()
    ui.page_header(f"SHIPPER · {user['sub']}", "신규 화물 등록",
                   "품목을 입력하면 Extreme Point 휴리스틱이 차량 적재를 3D로 시뮬레이션하고, 맞는 차종과 운임을 제안합니다.")

    if "_no_cold_pending" in st.session_state:  # 샘플 불러오기로 바뀐 콜드체인 값은 위젯 생성 전에 반영해야 함
        st.session_state.no_cold = st.session_state.pop("_no_cold_pending")

    # ---------------------------------------------------------------- ① 운송 정보
    with st.container(border=True):
        ui.section("① 운송 정보")
        regions = _regions(meta)
        c1, c2, c3 = st.columns([1.4, 1, 1])
        title = c1.text_input("화물명 / 품목", placeholder="예: 반도체 장비 11톤 (콜드체인)", key="no_title")
        origin = c2.selectbox("상차지 (출발)", regions, index=None, placeholder="지역 선택 · 입력해서 검색",
                              key="no_origin", help="도로 경로 · 운임 계산이 가능한 지역만 선택할 수 있습니다.")
        destination = c3.selectbox("하차지 (도착)", [r for r in regions if r != origin], index=None,
                                   placeholder="지역 선택 · 입력해서 검색", key="no_dest")
        c4, c5, c6, c7 = st.columns(4)
        cold = c4.toggle("콜드체인 (냉장 차량 필요)", key="no_cold")
        temp_set = c5.number_input("설정 온도 (°C)", -30.0, 10.0, -18.0, 0.5, disabled=not cold, key="no_temp")
        wait = c6.number_input("예상 상하차 대기 (분)", 0, 240, 30, 10, key="no_wait")
        urgent = c7.toggle("당일 긴급 특송", key="no_urgent")

    # ---------------------------------------------------------------- ② 적재 품목
    with st.container(border=True):
        ui.section("② 적재 품목 (Packing List)", "단위 mm · kg · 빈칸의 상부 허용하중 = 제한 없음")
        s1, s2, _ = st.columns([2, 1, 3], vertical_alignment="bottom")
        sample = s1.selectbox("샘플 시나리오", list(pack_meta["samples"]), key="no_sample")
        if s2.button("샘플 불러오기", icon=":material/inventory_2:", width="stretch"):
            smp = pack_meta["samples"][sample]
            st.session_state.no_df = _items_df(smp["items"])
            st.session_state._no_cold_pending = smp["cold_chain"]
            st.session_state.pop("no_editor", None)
            st.session_state.pop("no_plan", None)
            st.rerun()
        if "no_df" not in st.session_state:
            st.session_state.no_df = _items_df(pack_meta["samples"][next(iter(pack_meta["samples"]))]["items"])
        edited = st.data_editor(
            st.session_state.no_df, key="no_editor", num_rows="dynamic", hide_index=True, width="stretch",
            column_config={
                "품목": st.column_config.TextColumn(required=True, width=170),
                "길이(mm)": st.column_config.NumberColumn(min_value=50, max_value=13000, step=10, format="%d", width=78),
                "폭(mm)": st.column_config.NumberColumn(min_value=50, max_value=2500, step=10, format="%d", width=70),
                "높이(mm)": st.column_config.NumberColumn(min_value=50, max_value=3000, step=10, format="%d", width=78),
                "무게(kg)": st.column_config.NumberColumn(min_value=0.1, max_value=25000, step=0.5, format="%.1f", width=78),
                "수량": st.column_config.NumberColumn(min_value=1, max_value=300, step=1, format="%d", width=55),
                "세움 고정": st.column_config.CheckboxColumn(width=70, help="This side up — 눕히거나 뒤집지 않고 수직축 회전만 허용"),
                "적층 불가": st.column_config.CheckboxColumn(width=70, help="이 품목 위에는 아무것도 올리지 않음"),
                "파손주의": st.column_config.CheckboxColumn(width=70, help="파손주의 — 최상단에만 배치 (위 적재 금지)"),
                "냉동": st.column_config.CheckboxColumn(width=55, help="냉동 화물 — 최상단에만 배치 (냉기 순환 · 결로 방지)"),
                "상부 허용하중(kg)": st.column_config.NumberColumn(min_value=0, step=10, format="%d", width=110,
                                                            help="이 품목 위에 쌓일 수 있는 총 무게. 비우면 제한 없음"),
                "하차 순서": st.column_config.NumberColumn(min_value=1, max_value=9, step=1, format="%d", width=70,
                                                       help="1 = 가장 먼저 내림. 다중 하차지일 때 사용"),
            },
        )
        items, errors = _df_to_items(edited)
        for e in errors:
            st.warning(e)
        n_box = sum(i["qty"] for i in items)
        tot_w = sum(i["weight"] * i["qty"] for i in items)
        tot_v = sum(i["length"] * i["width"] * i["height"] * i["qty"] for i in items) / 1e9
        st.caption(f"품목 {len(items)}종 · 상자 {n_box}개 · 총 {tot_w:,.0f}kg · {tot_v:,.2f}m³ "
                   f"(최대 {pack_meta['max_boxes']}개)")

    # ---------------------------------------------------------------- ③ 시뮬레이션 설정
    trucks = [t for t in pack_meta["trucks"] if t["refrigerated"] or not cold]
    with st.container(border=True):
        ui.section("③ 3D 적재 시뮬레이션", "적재 규칙 R0~R10 + Extreme Point 탐색")
        a, b, c = st.columns([1.2, 1.3, 1.3])
        truck_opts = ["AUTO"] + [t["name"] for t in trucks]
        truck_sel = a.selectbox("차량", truck_opts, key="no_truck",
                                format_func=lambda v: "자동 추천 (가장 작은 적합 차량)" if v == "AUTO" else v)
        merit = b.segmented_control("배치 우선순위 (EP merit)", list(MERIT), format_func=MERIT.get,
                                    default="wall", key="no_merit") or "wall"
        sort = c.selectbox("정렬 휴리스틱", list(SORT), format_func=SORT.get, key="no_sort")
        d, e, f = st.columns([1.2, 1.3, 1.3])
        min_support = d.slider("최소 지지면 비율", 50, 100, 80, 5, format="%d%%", key="no_support",
                               help="상자 바닥 면적 중 아래 상자/바닥에 닿아야 하는 비율 (공중 부양 방지)") / 100
        lifo = e.toggle("R8 하차 순서(LIFO) 규칙", value=True, key="no_lifo",
                        help="먼저 내릴 화물의 위·앞(뒷문 쪽)을 나중에 내릴 화물이 막지 않게 함")
        f.caption("R9 안전율 · R10 좌우 균형은 항상 적용 — 한도(90%)까지만 싣고, 같은 줄 양쪽 중 무게중심이 "
                  "가운데로 오는 쪽에 놓습니다.")
        request = {"items": items, "truck_type": None if truck_sel == "AUTO" else truck_sel, "cold_chain": cold,
                   "options": {"merit": merit, "sort": sort, "min_support": min_support, "lifo": lifo}}
        run = st.button("3D 적재 시뮬레이션 실행", icon=":material/view_in_ar:", type="primary",
                        disabled=not items or bool(errors), width="stretch")
    if run:
        with st.spinner("Extreme Point 탐색 중..."):
            res = ui.act(api.post, "/packing/plan", request)
        if res:
            st.session_state.no_plan = {"hash": _hash(request), "res": res, "req": request}
            for k in ("no_step_ep",):
                st.session_state.pop(k, None)

    plan = st.session_state.get("no_plan")
    stale = plan is not None and plan["hash"] != _hash(request)
    if plan:
        if stale:
            st.warning("입력이 바뀌었습니다. 시뮬레이션을 다시 실행해야 등록할 수 있습니다.", icon="🔄")
        _results(plan["res"])

    # ---------------------------------------------------------------- ④ 운임 · 등록
    _register_section(plan, stale, title, origin, destination, cold, temp_set, wait, urgent)


# ======================================================================= results
def _results(res: dict):
    m, truck = res["metrics"], res["truck"]
    fails = [c for c in res["constraints"] if c["status"] == "fail"]
    if res["unplaced"]:
        ui.html(ui.banner(f"{truck['name']}에 {m['unplaced']}개를 싣지 못했습니다 — 더 큰 차량을 고르거나 품목을 나누세요.", "red"))
    elif fails:
        ui.html(ui.banner("필수 제약 위반: " + ", ".join(c["name"] for c in fails), "red"))
    else:
        label = "추천 차종" if res["auto"] else "선택 차종"
        ui.html(ui.banner(f"{label} · {truck['name']} — 상자 {m['placed']}개 전량 적재, 필수 제약 모두 충족", "green"))

    cog_check = next((c for c in res["constraints"] if c["key"] == "cog"), None)
    if cog_check and cog_check["status"] == "warn" and res["options"]["merit"] == "wall":
        st.caption("💡 무게가 운전석 쪽에 몰려 있습니다. 배치 우선순위를 **바닥 우선**으로 바꾸면 무게중심이 차체 가운데로 옵니다 "
                   "(단, 다중 하차지에서는 벽 쌓기가 하차 순서에 유리).")
    cog = m["cog"]
    ui.kpis([
        {"label": "차종", "value": truck["name"], "note": f"{truck['length']:,}×{truck['width']:,}×{truck['height']:,}mm"},
        {"label": "적재 수량", "value": f"{m['placed']}/{m['total']}", "unit": "개", "alert": m["unplaced"] > 0},
        {"label": "부피 적재율", "value": f"{m['volume_util'] * 100:.1f}", "unit": "%",
         "note": f"{m['loaded_volume_m3']}m³ / {truck['volume_m3']}m³"},
        {"label": "중량 적재율", "value": f"{m['weight_util'] * 100:.1f}", "unit": "%",
         "note": f"{m['loaded_weight']:,.0f} / {truck['max_weight_kg']:,.0f}kg"},
        {"label": "무게중심", "value": f"{(cog['x_ratio'] or 0) * 100:.0f}", "unit": "% 지점",
         "note": f"좌우 편차 {(cog['y_offset_ratio'] or 0) * 100:.1f}%"},
        {"label": "계산 시간", "value": m["elapsed_ms"], "unit": "ms", "note": "EP 휴리스틱"},
    ])

    left, right = st.columns([2.2, 1], gap="large")
    view, data = "ep", res
    with left:
        rows = data["placements"]
        violations = {v for c in data["constraints"] if c["status"] == "fail" for v in c["violations"]}
        if not rows:
            st.info("배치된 상자가 없습니다.")
            return
        s1, s2 = st.columns([4, 1.2], vertical_alignment="bottom")
        step = s1.slider("적재 단계 (상자를 놓는 순서대로 재생)", 1, len(rows), len(rows), key=f"no_step_{view}")
        show_eps = s2.toggle("EP 표시", value=True, key="no_show_eps")
        eps = chosen = None
        if show_eps and step - 1 < len(res["steps"]):
            eps = res["steps"][step - 1]
            chosen = rows[step - 1]["ep"]
        cur = rows[step - 1]
        rot = " · 회전 배치" if cur["rotated"] else ""
        sup = f" · 지지 {cur['support_ratio'] * 100:.0f}%" if cur.get("support_ratio") is not None else ""
        sup += " · R10 좌우 균형으로 반대편 배치" if cur.get("balanced") else ""
        ep_txt = f"후보 EP {len(eps)}개 중 ({cur['x']}, {cur['y']}, {cur['z']}) 선택" if eps else f"위치 ({cur['x']}, {cur['y']}, {cur['z']})"
        st.caption(f"**{step}번째** · {cur['name']} `{cur['item_id']}` — {ep_txt} · "
                   f"{cur['length']}×{cur['width']}×{cur['height']}mm{rot}{sup}")
        fig = packviz.figure(truck, rows, upto=step if step < len(rows) else None, eps=eps, chosen_ep=chosen,
                             violations=violations)
        st.plotly_chart(fig, key=f"pack3d_{view}", config={"displaylogo": False}, theme=None)
        if violations:
            st.caption(f"빨간 상자 {len(violations)}개 = 검증기가 찾은 규칙 위반 상자")
    with right:
        _constraint_card(data["constraints"], "적재 규칙 검증 (R0~R10)")
        if data["unplaced"]:
            ui.section(f"미적재 {len(data['unplaced'])}개")
            ui.html('<div class="ct-card">' + "".join(
                f'<div class="ct-item"><span class="ct-mono">{u["item_id"]}</span> '
                f'<span class="ct-strong">{u["name"]}</span><div class="ct-muted">{u["reason"]}</div></div>'
                for u in data["unplaced"][:12]) + "</div>")
        if res["auto"] and res["candidates"]:
            ui.section("차종별 검토", "작은 차량부터 시도")
            ui.html('<div class="ct-card">' + "".join(
                f'<div class="ct-item ct-row"><span class="ct-strong">{c["truck_type"]}</span>'
                f'{ui.pill(c.get("note", "-"), "green" if c["fits"] else ("gray" if not c["evaluated"] else "amber"))}</div>'
                for c in res["candidates"]) + "</div>")

    with st.expander("적재 순서표 · 적재 계획 JSON (팀 공통 포맷)"):
        rows = res["placements"]
        st.dataframe(pd.DataFrame([{
            "순서": r["load_order"], "ID": r["item_id"], "품목": r["name"], "하차": r["drop"],
            "x": r["x"], "y": r["y"], "z": r["z"], "길이": r["length"], "폭": r["width"], "높이": r["height"],
            "무게(kg)": r["weight_kg"], "지지율": r["support_ratio"], "상부하중(kg)": r["load_on_top"],
            "회전": "예" if r["rotated"] else "",
        } for r in rows]), hide_index=True, width="stretch", height=300,
            column_config={"지지율": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
        payload = {"order_id": "DRAFT", "unit": "mm",
                   "axis_note": "x=적재함 길이(0=운전석 쪽, 커질수록 뒷문 쪽), y=폭, z=높이(바닥=0). 좌표는 상자의 '모서리(최솟값 꼭짓점)' 기준",
                   "container": {k: truck[k] for k in ("name", "length", "width", "height", "max_weight_kg")},
                   "items": [{k: r[k] for k in ("item_id", "name", "x", "y", "z", "length", "width", "height",
                                                "weight_kg", "load_order")} for r in rows]}
        st.download_button("적재 계획 JSON 다운로드", json.dumps(payload, ensure_ascii=False, indent=2),
                           file_name="packing_plan.json", mime="application/json", icon=":material/download:")


def _constraint_card(checks: list[dict], title: str):
    ui.section(title)
    body = ""
    for c in checks:
        icon, tone = STATUS_ICON[c["status"]]
        fg, bg = ui.TONES[tone]
        body += (f'<div class="ct-item" style="display:flex;gap:10px;align-items:flex-start">'
                 f'<span style="flex:none;width:22px;height:22px;border-radius:50%;background:{bg};color:{fg};'
                 f'display:inline-flex;align-items:center;justify-content:center;font-weight:800;font-size:12px">{icon}</span>'
                 f'<div style="min-width:0"><div class="ct-strong" style="font-size:13.5px">'
                 f'{c["code"] + " " if c.get("code") else ""}{c["name"]}</div>'
                 f'<div class="ct-muted" style="font-size:12px">{c["detail"]}</div>'
                 f'<div class="ct-muted" style="font-size:11px;opacity:.75">{c["rule"]}'
                 f'{" · " + c["stage"] if c.get("stage") else ""}</div></div></div>')
    ui.html(f'<div class="ct-card" style="padding:12px 16px">{body}</div>')


# ======================================================================= register
def _estimate_card(est: dict):
    """AI 적정 운임 산정 근거 (도로거리 × 차종 단가 + 중량 · 조건 할증)."""
    rows = [("기본 운임", f"{est['truck_type']} · 도로 {est['distance_km']:.0f}km × {est['per_km']:,}원/km "
                      f"(최저 {est['min_fare']:,}원)", est["base"])]
    rows += [(f["label"], f["detail"], f["amount"]) for f in est["factors"] if f["amount"]]
    body = "".join(
        f'<div class="ct-row" style="padding:6px 0;border-top:1px solid #EEF0F4;font-size:13px">'
        f'<span><b class="ct-strong">{label}</b> <span class="ct-muted">{ui.escape(detail)}</span></span>'
        f'<span class="ct-strong" style="white-space:nowrap">{"+" if i else ""}{amount:,}원</span></div>'
        for i, (label, detail, amount) in enumerate(rows))
    ui.html(f'<div class="ct-card" style="padding:14px 16px;margin-bottom:10px">'
            f'<div class="ct-row" style="margin-bottom:6px"><span class="ct-strong">AI 적정 운임 (자동 산정)</span>'
            f'<span class="ct-big">{ui.won(est["recommended"])}</span></div>{body}'
            f'<div class="ct-muted" style="font-size:12px;margin-top:6px">시세 범위 {est["band"][0]:,} ~ {est["band"][1]:,}원 · '
            f'기사 배차 제안 카드에도 같은 금액이 표시됩니다.</div>{_market_line(est.get("market"))}</div>')


def _market_line(mk: dict | None) -> str:
    """날씨(모의 확률 분포) · 권역 수급 D/S · 서지 할증 근거 한 줄."""
    if not mk:
        return ""
    surge = f"서지 +{mk['surge']:.0%} 발동" if mk["surge"] else "서지 없음 (D/S ≤ 1.5)"
    return (f'<div class="ct-muted" style="font-size:12px;margin-top:4px">상차 권역 {mk["region"]} · 날씨 {mk["weather"]}'
            f'{" (+" + format(mk["weather_multiplier"], ".0%") + ")" if mk["weather_multiplier"] else ""} · '
            f'수요/공급 {mk["demand"]}/{mk["supply"]} = D/S {mk["ds_ratio"]} → {surge}'
            f'{" · 러시아워" if mk["rush_hour"] else ""}</div>')


def _register_section(plan, stale, title, origin, destination, cold, temp_set, wait, urgent):
    with st.container(border=True):
        ui.section("④ AI 운임 · 등록")
        res = plan["res"] if plan else None
        ok_plan = bool(res) and not stale and not res["unplaced"] and not any(
            c["status"] == "fail" for c in res["constraints"])
        formula = ui.load(api.get, "/meta").get("matching", {}).get("formula", "거리 + 적재율")
        if not ok_plan:
            st.caption("③ 시뮬레이션에서 전량 적재 + 필수 제약 충족 결과가 나와야 등록할 수 있습니다.")
        else:
            st.caption("등록하면 이 품목으로 기사마다 '그 기사 차량' 기준 적재 시뮬레이션을 다시 돌려, 전량 실리는 차량 중 "
                       f"상차지에서 가깝고 적재함을 잘 채우는 기사({formula})에게 자동으로 배차를 제안합니다.")
        truck_type = res["truck"]["name"] if res else None
        weight = res["metrics"]["loaded_weight"] if res else None
        max_item = max((i["length"] for i in plan["req"]["items"]), default=None) if plan else None

        # 예약 배차: 상차 시각을 정하면 그 시간대의 예상 날씨 · 수급으로 할증, 24시간 전 예약은 5% 할인
        r1, r2, r3 = st.columns([1, 1, 1], vertical_alignment="bottom")
        reserve = r1.toggle("예약 배차 (상차 시각 지정)", key="no_reserve")
        today = datetime.now()
        pick_date = r2.date_input("상차 날짜", value=today.date() + timedelta(days=1), min_value=today.date(),
                                  disabled=not reserve, key="no_pick_date")
        pick_time = r3.time_input("상차 시각", value=time(9, 0), step=1800, disabled=not reserve, key="no_pick_time")
        pickup_at = datetime.combine(pick_date, pick_time) if reserve else None
        if pickup_at and pickup_at <= today:
            st.warning("예약 시각이 이미 지났습니다. 즉시 배차로 계산합니다.")
            pickup_at = None

        # AI 적정 운임: 차종 · 상하차지 · 중량 · 조건이 정해지면 자동으로 계산 (바뀔 때만 다시 요청)
        est = None
        if ok_plan and origin and destination:
            key = _hash([truck_type, origin, destination, urgent, cold, int(wait), weight, max_item,
                         pickup_at.isoformat() if pickup_at else None])
            cache = st.session_state.get("no_est_cache")
            if cache and cache["key"] == key:
                est = cache["est"]
            else:
                est = ui.act(api.post, "/pricing/estimate", {
                    "truck_type": truck_type, "origin": origin, "destination": destination, "urgent": urgent,
                    "cold_chain": cold, "wait_minutes": int(wait), "weight_kg": weight, "max_item_mm": max_item,
                    "pickup_at": pickup_at.isoformat() if pickup_at else None})
                if est:
                    st.session_state.no_est_cache = {"key": key, "est": est}
                    st.session_state.no_price = est["recommended"]  # 조건이 바뀌면 추천가로 다시 맞춤
        st.session_state.setdefault("no_price", 400_000)
        if est:
            _estimate_card(est)
        elif ok_plan:
            st.caption("상차지 · 하차지를 선택하면 AI 적정 운임이 자동으로 계산됩니다.")
        c1, c2 = st.columns([1.3, 1], vertical_alignment="bottom")
        price = c1.number_input("희망 운임 (원)", min_value=10_000, max_value=10_000_000, step=5_000, key="no_price",
                                help="AI 적정 운임으로 자동 입력됩니다. 직접 바꿀 수 있어요.")
        if est and c2.button("AI 적정 운임으로 되돌리기", icon=":material/auto_awesome:", width="stretch",
                             disabled=price == est["recommended"]):
            st.session_state.pop("no_est_cache", None)  # 다음 렌더링에서 추천가로 다시 채움
            st.rerun()
        if est:
            gap = (price - est["recommended"]) / est["recommended"]
            if price < est["band"][0]:
                st.warning(f"AI 적정 운임보다 {-gap * 100:.0f}% 낮아 기사 거절 · 매칭 지연 가능성이 높습니다.")
            elif price > est["band"][1]:
                st.caption(f"AI 적정 운임보다 {gap * 100:.0f}% 높습니다 — 빠르게 수락될 가능성이 높아요.")

        if st.button("등록하고 매칭 시작", type="primary", width="stretch", disabled=not ok_plan,
                     icon=":material/send:"):
            if not (title and origin and destination):
                st.warning("① 운송 정보의 화물명, 상차지, 하차지를 입력해 주세요.")
                return
            req = plan["req"]
            created = ui.act(api.post, "/shipper/orders", {
                "title": title, "truck_type": truck_type, "origin": origin, "destination": destination,
                "price": int(price), "urgent": urgent, "cold_chain": cold,
                "temp_set": float(temp_set) if cold else None,
                "pickup_at": pickup_at.isoformat() if pickup_at else None,
                "items": req["items"], "packing_options": req["options"],
            })
            if created:
                for k in [k for k in st.session_state if k.startswith("no_")]:
                    del st.session_state[k]
                m = created["matching"]
                if m and m["state"] == "OFFERED":
                    result = (f"{m['driver']['name']} 기사({m['driver']['truck_type']}, 상차 {m['pickup_km']:.0f}km)에게 "
                              f"자동으로 배차를 제안했습니다.")
                    ui.flash(f"{created['id']} 등록 완료 — {result}")
                else:
                    ui.flash(f"{created['id']} 등록 완료 — 지금 제안할 수 있는 기사가 없어 관제센터가 확인합니다.", icon="⚠️")
                st.switch_page(nav.PAGES["dispatch"])
