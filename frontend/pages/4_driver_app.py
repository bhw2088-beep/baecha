"""차주(기사) 화면: 배차 제안 · AI 추천 리스트 · 내 운행 / 수익 · 화주 청구.

- 배차 제안: 자동 매칭이 '내 차량에 전량 실리고 상차지에서 가까운' 기사에게 한 건씩 보낸다 → 수락 / 거절.
- AI 추천 리스트: 내 차량에 실리는 매칭 대기 화물 전체를 같은 점수로 정렬 → 기사가 직접 골라 수락.
"""

import pandas as pd
import streamlit as st

import importlib

import api
import loading_guide
import ui

# 숫자로 시작하는 페이지 모듈은 일반 import 문으로 부를 수 없어 importlib 로 가져온다
_shipper_portal = importlib.import_module("pages.1_shipper_portal")
pod_dialog, render_order_detail = _shipper_portal.pod_dialog, _shipper_portal.render_order_detail

EVENT_TONE = {"accepted": "green", "picked": "green", "assigned": "brand", "declined": "gray", "expired": "amber"}


def _profile_header(p: dict):
    state_tone = {"공차 대기": "green", "배차 제안 도착": "brand", "상차지 이동": "amber", "운행중": "blue"}[p["state"]]
    rate = f'{p["accept_rate"]}%' if p["accept_rate"] is not None else "-"
    ui.html(f'<div class="ct-head"><div><div class="ct-eyebrow">DRIVER · {p["plate"]}</div>'
            f'<div class="ct-title">{p["name"]} 차주님</div>'
            f'<div class="ct-desc">{p["truck_type"]} · 현재 위치 {p["location"]}</div></div>'
            f'<div style="text-align:right">{ui.pill(p["state"], state_tone)}'
            f'<div class="ct-muted" style="margin-top:6px">배차 제안 수락률 <b class="ct-strong">{rate}</b>'
            f' · 받은 제안 {p["offers"]}건</div></div></div>')


# ======================================================================= 배차 제안 · 내 운행
def board():
    p = ui.load(api.get, "/driver/profile")
    _profile_header(p)
    a = p.get("approval") or {}
    if not p.get("approved", True):
        if a.get("status") == "REJECTED":
            st.error(f"가입이 반려되었습니다 · 사유: {a.get('reason')}. 서류를 보완해 다시 가입해 주세요.",
                     icon=":material/block:")
        else:
            st.warning("관제센터가 제출 서류(운전면허증 · 차량 사진)를 확인하고 있습니다. "
                       "승인되면 배차 제안과 AI 추천 리스트를 받을 수 있어요.", icon=":material/hourglass_top:")
    ui.kpis([
        {"label": "최근 30일 정산 수익", "value": ui.won(p["paid_30d"]), "note": f"입금 완료 {p['paid_30d_count']}건"},
        {"label": "미수금 · 승인 대기", "value": ui.won(p["receivable"]), "note": f"화주 심사 중 {p['receivable_count']}건"},
        {"label": "청구 필요", "value": p["unbilled_count"], "unit": "건", "alert": p["unbilled_count"] > 0,
         "note": "수익 · 화주 청구 메뉴에서 발송"},
        {"label": "이번 달 운행", "value": p["month_runs"], "unit": "회"},
    ])
    c1, c2 = st.columns([1, 2.4], vertical_alignment="center")
    c1.link_button("휴대폰 3D 적재 가이드", "http://localhost:8000/m/", icon=":material/smartphone:", width="stretch")
    c2.caption("휴대폰에서는 같은 와이파이로 http://<이 PC의 IP>:8000/m/ 에 접속하세요. "
               "상차 순서대로 상자를 3D 로 보여 줍니다.")

    if p["run"]:
        ui.section("내 운행", "상차 · 하차 처리는 현장에서 바로 하세요")
        run_panel(p["run"]["id"])
        run_actions(p["run"])
        st.write("")
        if p["run"]["packing"]:
            # 자동 갱신 fragment 밖에 둔다 — 단계 이동·3D 카메라 조작이 4초마다 초기화되지 않도록
            label = "상차 순서대로 실어 주세요" if p["run"]["status"] == "ASSIGNED" else "하차지마다 내릴 화물을 확인하세요"
            ui.section("적재 가이드", label)
            with st.container(border=True):
                loading_guide.render(p["run"]["id"], key=f"guide_{p['run']['id']}")
            st.write("")

    # 적재 계획 미리보기 대화상자는 자동 갱신 영역 밖(여기)에서 연다 — 갱신될 때 닫히지 않도록
    pending = st.session_state.pop("_plan_job", None)
    if pending:
        plan_dialog(pending)

    if not p["run"]:
        offer_panel()
        st.write("")
        recommend_panel()
    _offer_history()


@st.fragment(run_every="3s")
def offer_panel():
    """나에게 온 배차 제안을 3초마다 확인한다. 수락하면 '내 운행'으로 바뀐다."""
    try:
        offer = api.get("/driver/offer")
        run = api.get("/driver/profile")["run"]
    except api.ApiError as e:
        st.error(str(e))
        return
    if run:  # 관제가 나에게 직접 배차한 경우 등
        st.session_state.pop("_last_offer", None)
        st.toast("배차가 확정되었습니다.", icon="🔄")
        st.rerun(scope="app")

    last = st.session_state.get("_last_offer")
    if last and (not offer or offer["id"] != last):
        st.toast("응답 시간이 지나 제안이 다음 기사에게 넘어갔습니다.", icon="⏱️")
    if offer and offer["id"] != last:
        st.toast(f"새 배차 제안 · {offer['title']}", icon="📦")
    st.session_state._last_offer = offer["id"] if offer else None

    ui.section("배차 제안", "자동 매칭 · 내 차량에 전량 실리는 화물 중 상차지가 가까운 순 · 3초마다 확인")
    if not offer:
        ui.html('<div class="ct-card"><div class="ct-strong">공차 대기 중입니다</div>'
                '<div class="ct-muted" style="margin-top:4px">화주가 화물을 등록하면, 내 차량 기준으로 3D 적재 시뮬레이션을 돌려 '
                '전량 실리고 상차지에서 가까운 기사에게 자동으로 배차 제안이 옵니다. 제안이 오면 이 자리에 표시됩니다.</div></div>')
        return
    _offer_card(offer)


def _offer_card(o: dict):
    m = o["matching"]
    left = m.get("expires_in", 0)
    pct = 100 * left / max(m["timeout"], 1)
    tone = "#B42318" if left <= 15 else "#B54708" if left <= 30 else "#4F46E5"
    tags = [ui.pill(f"{m['offer_round']}번째 제안" if m["offer_round"] > 1 else "AI 자동 매칭", "brand")]
    if o["urgent"]:
        tags.append(ui.pill("긴급 배차", "red"))
    if o["cold_chain"]:
        tags.append(ui.pill("콜드체인", "teal"))
    if o["incentive"]:
        tags.append(ui.pill(f"관제 보조금 +{o['incentive']:,}원", "green"))
    pk = o.get("packing")
    util = m.get("util")
    with st.container(border=True):
        ui.html(
            f'<div class="ct-row" style="align-items:flex-start"><div style="min-width:0">'
            f'<div style="display:flex;gap:6px;flex-wrap:wrap">{" ".join(tags)}</div>'
            f'<div class="ct-big" style="margin-top:8px">{o["title"]}</div>'
            f'<div class="ct-muted">{o["id"]} · {o["shipper"]} · 요청 {o["truck_type"]}</div></div>'
            f'<div style="text-align:right;flex-shrink:0"><div class="ct-muted" style="font-size:11.5px">응답 남은 시간</div>'
            f'<div style="font-size:30px;font-weight:800;color:{tone};font-variant-numeric:tabular-nums">{left}초</div></div></div>'
            f'<div class="ct-track" style="margin:8px 0 2px"><div class="fill" style="width:{pct:.0f}%;background:{tone}"></div></div>'
            + ui.route_bar(o["origin"], o["destination"], 0, f"상차지까지 {m['pickup_km']:.0f}km",
                           f"운송 {o['distance_km']:.0f}km")
            + ui.stats([
                ("제시 운임", ui.won(o["fare"])),
                ("내 차량 적재율", f"{util * 100:.0f}%" if util is not None else "-"),
                ("매칭 점수", f"{m['score'] * 100:.0f}점" if m.get("score") is not None else "-"),
            ])
            + ui.fare_compare(o.get("fare_est"))
            + (f'<div class="ct-muted" style="margin-top:10px;font-size:12.5px">적재 계획 · 상자 {pk["placed"]}개 · '
               f'{pk["loaded_weight"]:,.0f}kg · 부피 {pk["volume_util"] * 100:.0f}% · 중량 {pk["weight_util"] * 100:.0f}% '
               f'({pk["truck"]} 기준)</div>' if pk else "")
            + (f'<div class="ct-muted" style="font-size:12px;margin-top:4px">AI 수락 예측 {m["ml_prob"] * 100:.0f}% '
               f'(LightGBM · 가상 데이터 기반) · 규칙 점수 {m["rule_score"] * 100:.0f}점</div>'
               if m.get("ml_prob") is not None else "")
            + f'<div class="ct-muted" style="font-size:12px;margin-top:4px">점수 = {m["formula"]} (반경 {m["radius_km"]}km 기준)'
              f' · 헝가리안 최적 배정 · 후보 {m["candidates"]}대 중 {m.get("rank") or 1}순위</div>')
        c1, c2, c3 = st.columns([1, 1, 1.4])
        if pk and c1.button("적재 계획 보기", key=f"plan_{o['id']}", icon=":material/view_in_ar:", width="stretch"):
            st.session_state._plan_job = o
            st.rerun(scope="app")
        if c2.button("거절", key=f"decline_{o['id']}", icon=":material/close:", width="stretch"):
            if ui.act(api.post, f"/driver/offer/{o['id']}/decline", success="제안을 거절했습니다. 다음 순위 기사에게 넘어갑니다."):
                st.session_state._last_offer = None
                st.rerun(scope="app")
        if c3.button("수락하고 배차 받기", key=f"accept_{o['id']}", type="primary", icon=":material/check:",
                     width="stretch"):
            if ui.act(api.post, f"/driver/offer/{o['id']}/accept", success="배차가 확정되었습니다. 상차지로 이동해 주세요."):
                st.session_state._last_offer = None
                st.rerun(scope="app")


@st.fragment(run_every="5s")
def recommend_panel():
    """AI 추천 리스트: 내 차량에 전량 실리는 매칭 대기 화물을 매칭 점수 순으로. 기사가 직접 골라 수락할 수 있다."""
    try:
        recs = api.get("/driver/recommendations")
    except api.ApiError as e:
        st.error(str(e))
        return
    formula = recs[0]["matching"]["formula"] if recs else "거리 + 적재율"
    ui.section(f"AI 매칭 추천 리스트 · {len(recs)}",
               f"내 차량에 전량 실리는 매칭 대기 화물 · 점수 = {formula} · 직접 골라 수락 · 5초마다 갱신")
    if not recs:
        ui.html('<div class="ct-card ct-muted">지금 내 차량에 실을 수 있는 매칭 대기 화물이 없습니다.</div>')
        return
    # 2열 카드 (휴대폰처럼 좁은 화면에서는 Streamlit 이 열을 자동으로 세로로 쌓는다)
    for row_start in range(0, len(recs), 2):
        cols = st.columns(2, gap="medium")
        for col, o in zip(cols, recs[row_start:row_start + 2]):
            with col:
                _rec_card(o)


def _rec_card(o: dict):
    r = o["rec"]
    locked = r["offered_to_other"] is not None
    tags = [ui.pill(f"추천 {r['rank']}순위", "brand" if r["rank"] == 1 and not locked else "gray")]
    if not r["in_radius"]:
        tags.append(ui.pill("자동 제안 반경 밖", "amber"))
    if r["declined_by_me"]:
        tags.append(ui.pill("이전에 거절", "gray"))
    if o["urgent"]:
        tags.append(ui.pill("긴급", "red"))
    if o["cold_chain"]:
        tags.append(ui.pill("콜드체인", "teal"))
    if o["incentive"]:
        tags.append(ui.pill(f"보조금 +{o['incentive']:,}원", "green"))
    score_color = "#067647" if r["score"] >= 0.6 else "#B54708" if r["score"] >= 0.3 else "#667085"
    pk = o.get("packing")
    with st.container(border=True):
        ui.html(
            f'<div class="ct-row" style="align-items:flex-start"><div style="min-width:0">'
            f'<div style="display:flex;gap:6px;flex-wrap:wrap;min-height:22px">{" ".join(tags)}</div>'
            f'<div class="ct-strong" style="font-size:16px;margin-top:8px">{o["title"]}</div>'
            f'<div class="ct-muted">{o["id"]} · {o["shipper"]} · 대기 {ui.duration(o["wait_seconds"])}</div></div>'
            f'<div style="text-align:right;flex-shrink:0"><div class="ct-muted" style="font-size:11.5px">매칭 점수</div>'
            f'<div style="font-size:24px;font-weight:800;color:{score_color}">{r["score"] * 100:.0f}</div></div></div>'
            + ui.route_bar(o["origin"], o["destination"], 0, f"상차지까지 {r['pickup_km']:.0f}km",
                           f"운송 {o['distance_km']:.0f}km")
            + ui.stats([("제시 운임", ui.won(o["fare"])),
                        ("내 차량 적재율", f"{r['util'] * 100:.0f}%"),
                        ("AI 수락 예측", f"{r['ml_prob'] * 100:.0f}%" if r.get("ml_prob") is not None else "-")])
            + ui.fare_compare(o.get("fare_est"))
            + (f'<div class="ct-muted" style="font-size:12px;margin-top:6px">적재 상자 {pk["placed"]}개 · '
               f'{pk["loaded_weight"]:,.0f}kg · AI 수락 예측은 LightGBM · 가상 데이터 기반</div>' if pk else ""))
        if locked:
            st.caption(f"🔒 {r['offered_to_other']} 기사가 자동 배차 제안에 응답하는 중입니다 (남은 {r['offer_left']}초). "
                       "응답이 없으면 수락할 수 있게 됩니다.")
        c1, c2 = st.columns([1, 1.4])
        if pk and c1.button("적재 계획 보기", key=f"recplan_{o['id']}", icon=":material/view_in_ar:", width="stretch"):
            st.session_state._plan_job = o
            st.rerun(scope="app")
        if c2.button("수락하고 배차 받기", key=f"pick_{o['id']}", type="primary", icon=":material/check:",
                     width="stretch", disabled=locked):
            if ui.act(api.post, f"/driver/recommendations/{o['id']}/accept",
                      success=f"{o['title']} 배차가 확정되었습니다. 상차지로 이동해 주세요."):
                st.session_state._last_offer = None
                st.rerun(scope="app")


def _offer_history():
    rows = ui.load(api.get, "/driver/offers/history")
    if not rows:
        return
    st.write("")
    ui.section("최근 배차 제안 결과", f"{len(rows)}건")
    ui.html('<div class="ct-card">' + "".join(
        f'<div class="ct-item ct-row"><div style="min-width:0"><span class="ct-mono">{r["order_id"]} · '
        f'{ui.dt_text(r["at"], "%m.%d %H:%M")}</span><div class="ct-strong">{r["title"]}</div>'
        f'<div class="ct-muted">{r["origin"]} → {r["destination"]} · {ui.won(r["fare"])}</div></div>'
        f'{ui.pill(r["label"], EVENT_TONE.get(r["event"], "gray"))}</div>' for r in rows) + "</div>")


@st.dialog("적재 계획 미리보기", width="large")
def plan_dialog(j: dict):
    ui.html(f'<div class="ct-muted">{j["id"]} · {j["origin"]} → {j["destination"]}</div>'
            f'<div class="ct-big">{j["title"]}</div>')
    st.caption("내 차량 기준으로 다시 계산한 적재 계획입니다. 자동 매칭은 이 계획이 전량 · 필수 제약 충족일 때만 제안을 보냅니다.")
    loading_guide.render(j["id"], key=f"preview_{j['id']}", height=420)


@st.fragment(run_every="4s")
def run_panel(order_id: str):
    try:
        o = api.get(f"/orders/{order_id}")
    except api.ApiError as e:
        st.error(str(e))
        return
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        render_order_detail(o, show_map=False)
    with right:
        ui.route_map(o, height=300)


def run_actions(o: dict):
    """운행 버튼은 자동 갱신 fragment 밖에 둔다 (갱신될 때 열린 대화상자가 닫히지 않도록)."""
    c1, _ = st.columns([1.3, 1], gap="large")
    if o["status"] == "ASSIGNED":
        if c1.button("상차 완료 · 운행 시작", icon=":material/play_arrow:", type="primary", width="stretch"):
            if ui.act(api.post, f"/driver/runs/{o['id']}/start", success="운행을 시작합니다. 안전 운전하세요!"):
                st.rerun()
    elif o["status"] == "IN_TRANSIT":
        if c1.button("하차 완료 · 전자인수증 서명", icon=":material/draw:", type="primary", width="stretch"):
            complete_dialog(o)


@st.dialog("하차 완료 · 전자인수증 (e-POD)")
def complete_dialog(o: dict):
    ui.html(f'<div class="ct-muted">{o["id"]} · {o["title"]}</div><div class="ct-big">{o["destination"]}</div>')
    receiver = st.text_input("인수자 성함", placeholder="물류센터 수령인 이름")
    confirm = st.checkbox("화물을 이상 없이 인계했고, 인수자가 서명에 동의했습니다.")
    if st.button("서명 완료", type="primary", width="stretch", disabled=not (receiver and confirm)):
        if ui.act(api.post, f"/driver/runs/{o['id']}/complete", {"receiver": receiver},
                  success="배송 완료! 전자인수증이 발급되었습니다. 이제 운송비를 청구할 수 있어요."):
            st.rerun()


# ======================================================================= 수익 · 화주 청구
def earnings():
    p = ui.load(api.get, "/driver/profile")
    _profile_header(p)
    ui.kpis([
        {"label": "최근 30일 정산 수익", "value": ui.won(p["paid_30d"]), "note": f"입금 완료 {p['paid_30d_count']}건"},
        {"label": "화주 승인 대기 (미수금)", "value": ui.won(p["receivable"]), "note": f"{p['receivable_count']}건 심사 중"},
        {"label": "전자인수증 발급률", "value": p["pod_rate"], "unit": "%", "note": "서명 완료 · 해시 등록"},
        {"label": "평균 정산 소요", "value": p["avg_settle_days"] or "-", "unit": "일", "note": "청구 → 입금"},
    ])

    jobs = ui.load(api.get, "/driver/completed")
    ui.section("운송 완료 내역", "완료 건의 인수증을 확인하고 화주에게 운송비 청구서를 보내세요")
    if not jobs:
        st.info("아직 완료된 운송이 없습니다.")
        return
    df = pd.DataFrame([{
        "화물 ID": j["id"], "완료일": ui.dt_text(j["delivered_at"], "%Y.%m.%d"), "화물": j["title"],
        "화주": j["shipper"], "구간": f"{j['origin'].split()[0]} → {j['destination'].split()[0]}",
        "기본 운임": j["fare"], "추가·통행료": j["extra_fee"] + j["toll_fee"], "합계": j["total_fee"],
        "청구 상태": j["billing_label"],
    } for j in jobs])
    money = st.column_config.NumberColumn(format="%,d원", width=90)
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="earnings_table",
                         column_config={"화물 ID": st.column_config.TextColumn(width=130),
                                        "완료일": st.column_config.TextColumn(width=85),
                                        "화물": st.column_config.TextColumn(width=200),
                                        "화주": st.column_config.TextColumn(width=95),
                                        "구간": st.column_config.TextColumn(width=110),
                                        "기본 운임": money, "추가·통행료": money, "합계": money,
                                        "청구 상태": st.column_config.TextColumn(width=80)})
    rows = event.selection.rows
    unbilled = [j for j in jobs if j["billing"] in ("UNBILLED", "REFUNDED")]  # 결제 취소된 건은 재청구
    if not rows or rows[0] >= len(jobs):
        if unbilled:
            st.caption(f"청구 필요 {len(unbilled)}건 — 행을 선택해 청구서를 발송하세요.")
        return
    j = jobs[rows[0]]
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("전자인수증", icon=":material/description:", width="stretch"):
        pod_dialog(j)
    if j["billing"] in ("UNBILLED", "REFUNDED"):
        if c2.button("화주 청구서 발송", icon=":material/send:", type="primary", width="stretch"):
            invoice_dialog(j)
    else:
        c2.button(j["billing_label"], disabled=True, width="stretch")


@st.dialog("화주 운송비 청구서", width="large")
def invoice_dialog(j: dict):
    ui.html(f'<div class="ct-row"><div><span class="ct-mono">{j["id"]}</span><div class="ct-big">{j["title"]}</div>'
            f'<div class="ct-muted">청구 대상 · {j["shipper"]} · 인수자 서명 {j["pod"]["receiver"]}</div></div>'
            f'{ui.pill("e-POD 첨부됨", "green")}</div>')
    c1, c2, c3 = st.columns(3)
    c1.number_input("기본 운임 (원)", value=j["fare"], disabled=True)
    extra = c2.number_input("하차대기 · 경유 추가금", 0, 5_000_000, j["extra_fee"], 5_000)
    toll = c3.number_input("통행료 · 기타 비용", 0, 1_000_000, j["toll_fee"], 1_000)
    total = j["fare"] + extra + toll
    ui.html(f'<div class="ct-card" style="margin:6px 0 10px"><div class="ct-row"><span class="ct-muted">최종 청구 합계 '
            f'(VAT 별도 · 전자세금계산서 자동 발급)</span><span class="ct-big">{ui.won(total)}</span></div></div>')
    memo = st.text_area("화주 전달 메모", max_chars=300, placeholder="예: 하차지 대기 40분 발생, 통행료 영수증 첨부")
    if st.button("청구서 발송", type="primary", width="stretch"):
        if ui.act(api.post, f"/driver/completed/{j['id']}/invoice",
                  {"extra_fee": int(extra), "toll_fee": int(toll), "memo": memo},
                  success="화주 담당자에게 청구서를 발송했습니다."):
            st.rerun()
