"""
관리자 컴플라이언스 (Phase 7): 차주 가입 승인(사업자 Mod-10 · 노란 번호판 · 서류) · 리뷰 · 블랙리스트 ·
감사 로그(SHA-256 해시 체인 위변조 검사) · 모의 PG 결제 내역/취소.
"""

import pandas as pd
import streamlit as st

import api
import ui

EVENT_TONE = {"create": "gray", "assign": "brand", "start": "blue", "deliver": "teal", "request": "amber",
              "pay": "green", "settle": "green", "refund": "red", "unsettle": "red"}
DOC_LABEL = {"license": "운전면허증", "cargo_license": "화물운송종사 자격증", "vehicle": "차량등록증 · 번호판"}
PLATE_LABEL = {"COMMERCIAL_YELLOW": "사업용(노란)", "PRIVATE_WHITE": "자가용(흰색)", "RENTAL": "렌터카", "INVALID": "형식 오류"}


def page():
    ui.page_header("CONTROL CENTER", "컴플라이언스",
                   "차주 가입 서류를 심사하고, 모든 상태 변경 · 재무 변경 이력과 결제 내역을 확인합니다.")
    drivers = ui.load(api.get, "/admin/compliance/drivers")
    pending = [d for d in drivers if d["status"] == "PENDING"]
    payments = ui.load(api.get, "/admin/payments")
    audit = ui.load(api.get, "/admin/audit", limit=500)
    rep = ui.load(api.get, "/admin/reputation")
    blocked = [r for r in rep if r["blacklisted"]]
    ui.kpis([
        {"label": "가입 승인 대기", "value": len(pending), "unit": "명", "alert": bool(pending),
         "note": "승인 전에는 배차를 받을 수 없음"},
        {"label": "배차 불가 기사", "value": sum(1 for d in drivers if not d["dispatchable"]), "unit": "명",
         "note": f"이용 제한 {len(blocked)} · 번호판 부적합 {sum(1 for d in drivers if d['compliance']['plate_type'] != 'COMMERCIAL_YELLOW')}"},
        {"label": "감사 로그", "value": len(audit), "unit": "건", "note": "최근 500건 · SHA-256 해시 체인"},
        {"label": "재무 변경", "value": sum(1 for e in audit if e["financial"]), "unit": "건",
         "note": "청구 · 결제 · 취소"},
        # 카드 폭에서 잘리지 않게 만원 단위로 (예: 265.3만원)
        {"label": "결제 승인 (모의 PG)", "unit": "원",
         "value": ui.manwon(sum(p["amount"] for p in payments if p["status"] == "APPROVED")),
         "note": f"취소 {sum(1 for p in payments if p['status'] == 'CANCELLED')}건"},
    ])
    po = ui.load(api.get, "/admin/payouts")
    t1, t4, t2, t3, t5 = st.tabs([f"차주 가입 승인 · {len(pending)}", f"리뷰 · 블랙리스트 · {len(blocked)}",
                                  "감사 로그 (해시 체인)", "결제 내역 · 취소 (모의 PG)",
                                  f"기사 수익금 송금 · {len(po['pending'])}"])
    with t1:
        _drivers_tab(drivers)
    with t4:
        _reviews_tab(rep)
    with t2:
        _audit_tab(audit)
    with t3:
        _payments_tab(payments)
    with t5:
        _payouts_tab(po)


# ------------------------------------------------------------------ 기사 수익금 일괄 송금
def _payouts_tab(po: dict):
    st.caption(f"화주 결제(모의 PG)가 끝난 운송비를 기사별로 묶어 한 번에 송금합니다. 플랫폼 수수료 {po['fee_rate']:.0%} 를 뺀 금액이 "
               "송금되며, 모의 이체라 실제 출금은 없습니다. 송금된 건은 결제 취소(환불)를 할 수 없습니다.")
    pend = po["pending"]
    if pend:
        ui.kpis([
            {"label": "송금 대기 기사", "value": len(pend), "unit": "명"},
            {"label": "송금 대기 건", "value": sum(r["count"] for r in pend), "unit": "건"},
            {"label": "결제 합계", "value": ui.won(sum(r["gross"] for r in pend))},
            {"label": "송금 예정액", "value": ui.won(sum(r["net"] for r in pend)),
             "note": f"수수료 {ui.won(sum(r['fee'] for r in pend))}"},
        ])
        st.dataframe(pd.DataFrame([{
            "기사": f"{r['driver']} ({r['driver_id']})", "차량": r["plate"], "건수": r["count"],
            "결제 합계": r["gross"], "수수료": r["fee"], "송금액": r["net"], "화물": ", ".join(r["orders"]),
        } for r in pend]), hide_index=True, width="stretch", height=ui.table_height(len(pend)), column_config={
            "기사": st.column_config.TextColumn(width=120), "차량": st.column_config.TextColumn(width=110),
            "건수": st.column_config.NumberColumn(format="%d건", width=55),
            **{k: st.column_config.NumberColumn(format="%,d원", width=100) for k in ("결제 합계", "수수료", "송금액")},
            "화물": st.column_config.TextColumn(width=300, help="칸에 마우스를 올리면 전체 화물 ID")})
        if st.button(f"{len(pend)}명에게 일괄 송금 (모의)", type="primary", icon=":material/payments:"):
            if ui.act(api.post, "/admin/payouts/run", json={"driver_ids": []},
                      success=f"{len(pend)}명에게 {ui.won(sum(r['net'] for r in pend))} 송금했습니다 (모의 이체)."):
                st.rerun()
    else:
        st.info("송금할 금액이 없습니다. 화주가 청구서를 승인(결제)하면 여기에 쌓입니다.", icon=":material/payments:")
    ui.section("송금 내역", f"{len(po['history'])}건")
    if po["history"]:
        st.dataframe(pd.DataFrame([{
            "송금 번호": h["id"], "일시": ui.dt_text(h["at"], "%m.%d %H:%M"), "기사": h["driver"], "건수": h["count"],
            "결제 합계": h["gross"], "수수료": h["fee"], "송금액": h["net"], "계좌": h["bank"], "거래번호": h["tid"],
            "처리": h["by"],
        } for h in po["history"]]), hide_index=True, width="stretch", height=ui.table_height(len(po["history"])),
            column_config={
            "송금 번호": st.column_config.TextColumn(width=110), "일시": st.column_config.TextColumn(width=80),
            "기사": st.column_config.TextColumn(width=60), "건수": st.column_config.NumberColumn(format="%d건", width=50),
            **{k: st.column_config.NumberColumn(format="%,d원", width=95) for k in ("결제 합계", "수수료", "송금액")},
            "계좌": st.column_config.TextColumn(width=130), "거래번호": st.column_config.TextColumn(width=125),
            "처리": st.column_config.TextColumn(width=80)})


# ------------------------------------------------------------------ 가입 승인
def _drivers_tab(drivers: list[dict]):
    pending = [d for d in drivers if d["status"] == "PENDING"]
    if not pending:
        st.info("승인 대기 중인 가입 신청이 없습니다. 회원가입 화면에서 차주로 가입하면 여기에 나타납니다.",
                icon=":material/verified_user:")
    for d in pending:
        with st.container(border=True):
            docs = " · ".join(f"{DOC_LABEL.get(x['kind'], x['kind'])} {x['filename']} "
                              f"({x['size'] / 1024:.0f}KB)" for x in d["documents"]) or "제출 서류 없음"
            cp, biz = d["compliance"], d.get("biz_check") or {}
            ton = f"{cp['tonnage']:g}톤"
            checks = (f'{ui.pill(PLATE_LABEL.get(cp["plate_type"], cp["plate_type"]), "green" if cp["plate_type"] == "COMMERCIAL_YELLOW" else "red")} '
                      f'{ui.pill("사업자 " + biz.get("label", "-"), "green" if biz.get("ok") else "red")} '
                      f'{ui.pill(ton, "gray")}')
            ui.html(f'<div class="ct-row"><div><div class="ct-strong" style="font-size:16px">{d["name"]} · '
                    f'{d["truck_type"]} · {d["plate"]}</div><div class="ct-muted">{d["id"]} · {d["email"] or "-"} · '
                    f'{d["phone"] or "-"} · 대기 지역 {d["location"]} · 사업자 {d.get("biz_no") or "-"} '
                    f'({biz.get("source_label", "-")})</div>'
                    f'<div style="margin-top:6px">{checks}</div>'
                    f'<div class="ct-muted" style="margin-top:6px">제출 서류: {docs}</div>'
                    + (f'<div style="color:#B42318;font-size:12.5px;margin-top:4px">⚠ {" / ".join(cp["issues"])}</div>'
                       if cp["issues"] else "") + f'</div><div>{ui.pill("승인 대기", "amber")}</div></div>')
            c1, c2, c3 = st.columns([2.2, 1, 1], vertical_alignment="bottom")
            reason = c1.text_input("반려 사유", key=f"reason_{d['id']}", placeholder="반려할 때만 입력",
                                   label_visibility="collapsed")
            if c2.button("반려", key=f"reject_{d['id']}", width="stretch"):
                if ui.act(api.post, f"/admin/compliance/drivers/{d['id']}", json={"approve": False, "reason": reason},
                          success=f"{d['name']} 기사 가입을 반려했습니다."):
                    st.rerun()
            if c3.button("승인", key=f"approve_{d['id']}", type="primary", width="stretch"):
                if ui.act(api.post, f"/admin/compliance/drivers/{d['id']}", json={"approve": True},
                          success=f"{d['name']} 기사를 승인했습니다. 이제 자동 매칭 후보입니다."):
                    st.rerun()
    ui.section("전체 차주", f"{len(drivers)}명")
    st.caption("배차 가능 = 관제 승인 + 사업용 노란 번호판(바 · 사 · 아 · 자) + 이용 제한 아님. "
               "사업자번호는 Mod-10 검증 + 국세청 상태(키 없으면 모의 판정).")
    def _reasons(d: dict) -> str:
        # 서류 · 번호판 문제 + 이용 제한 + 반려 사유를 ' / ' 로 (앞에 ' / ' 가 붙지 않게 목록으로 모아서 잇는다)
        why = list(d["compliance"]["issues"]) + (["이용 제한"] if d["blacklisted"] else [])
        if not why and d.get("reason"):
            why.append(d["reason"])
        return " / ".join(why) or "-"

    c = st.column_config.TextColumn
    st.dataframe(pd.DataFrame([{
        "기사": d["name"], "ID": d["id"], "차종": d["truck_type"], "톤수": f'{d["compliance"]["tonnage"]:g}톤',
        "차량": d["plate"], "번호판": PLATE_LABEL.get(d["compliance"]["plate_type"], "-"),
        "심사": ui.state_text(d["status"]),
        "평점": ui.rating_text((d.get("rating") or {}).get("avg")),
        "리뷰": ui.num((d.get("rating") or {}).get("count"), "{}건"),
        "배차": "가능" if d["dispatchable"] else "불가", "사유": _reasons(d),
    } for d in drivers]), hide_index=True, width="stretch", height=ui.table_height(len(drivers), 20), column_config={
        "기사": c(width=65), "ID": c(width=60), "차종": c(width=95), "톤수": c(width=50), "차량": c(width=110),
        "번호판": c(width=95), "심사": c(width=65), "평점": c(width=55), "리뷰": c(width=45),
        "배차": c(width=45, help="배차 가능 여부"), "사유": c(width=240, help="칸에 마우스를 올리면 전체 사유")})


# ------------------------------------------------------------------ 리뷰 · 블랙리스트
def _reviews_tab(rep: list[dict]):
    tags = ui.load(api.get, "/reviews/tags")
    st.caption(f"화주 ↔ 기사 상호 별점 · 태그 평가. 기사 평점은 매칭 AI 피처로 쓰입니다. {tags['blacklist_rule']}")
    c = st.column_config.TextColumn
    st.dataframe(pd.DataFrame([{
        "기사": f"{r['name']} ({r['id']})", "차량": r["plate"], "평점": ui.rating_text(r["avg"]),
        "리뷰": ui.num(r["count"], "{}건"), "주요 태그": ", ".join(r["top_tags"]) or "-",
        "상태": "이용 제한" if r["blacklisted"] else ("주의" if r["at_risk"] else "정상"),
        "제한 사유": ((r["blacklist"] or {}).get("reason") or "-") if r["blacklisted"] else "-",
    } for r in rep]), hide_index=True, width="stretch", height=ui.table_height(len(rep), 20), column_config={
        "기사": c(width=120), "차량": c(width=110), "평점": c(width=60), "리뷰": c(width=50),
        "주요 태그": c(width=240), "상태": c(width=70), "제한 사유": c(width=220)})
    with st.container(border=True):
        ui.section("이용 제한 · 해제", "제한하면 매칭 후보에서 바로 빠지고 진행 중인 제안도 회수됩니다")
        c1, c2, c3, c4 = st.columns([1.6, 2.2, 0.8, 0.8], vertical_alignment="bottom")
        pick = c1.selectbox("기사", rep, format_func=lambda r: f"{r['name']} · {r['plate']}"
                            + (" (제한 중)" if r["blacklisted"] else ""), key="bl_pick")
        reason = c2.text_input("사유", placeholder="예) 화물 파손 반복 · 연락 두절", key="bl_reason")
        if c3.button("제한", type="primary", width="stretch", disabled=pick is None or pick["blacklisted"]):
            if ui.act(api.post, f"/admin/drivers/{pick['id']}/blacklist", json={"on": True, "reason": reason},
                      success=f"{pick['name']} 기사를 이용 제한했습니다."):
                st.rerun()
        if c4.button("해제", width="stretch", disabled=pick is None or not pick["blacklisted"]):
            if ui.act(api.post, f"/admin/drivers/{pick['id']}/blacklist", json={"on": False, "reason": reason or "관제 해제"},
                      success=f"{pick['name']} 기사의 이용 제한을 풀었습니다."):
                st.rerun()
    reviews = ui.load(api.get, "/admin/reviews")
    ui.section("최근 리뷰", f"{len(reviews)}건")
    if reviews:
        st.dataframe(pd.DataFrame([{
            "일시": ui.dt_text(r["at"], "%m.%d %H:%M"), "화물": r["order_id"],
            "방향": "화주 → 기사" if r["to_role"] == "driver" else "기사 → 화주",
            "작성": r["writer"], "대상": r["target"], "별점": "★" * r["stars"] + "☆" * (5 - r["stars"]),
            "태그": ", ".join(r["tags"]) or "-", "코멘트": r["comment"] or "-",
        } for r in reviews]), hide_index=True, width="stretch", height=ui.table_height(len(reviews)), column_config={
            "일시": c(width=80), "화물": c(width=125), "방향": c(width=85), "작성": c(width=70), "대상": c(width=90),
            "별점": c(width=80), "태그": c(width=180), "코멘트": c(width=200, help="칸에 마우스를 올리면 전체 코멘트")})


# ------------------------------------------------------------------ 감사 로그
def _audit_tab(audit: list[dict]):
    c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
    q = c1.text_input("화물 ID 검색", placeholder="예: ORD-20261116-01", label_visibility="collapsed")
    fin = c2.toggle("재무 변경만 (청구 · 결제 · 취소)")
    rows = [e for e in audit if (not q or q.strip().upper() in e["order_id"]) and (not fin or e["financial"])]
    st.caption("상태는 engines/fsm/order_fsm.py 의 전이표로만 바뀌고, 모든 전이 · 관제 조치가 여기에 남습니다. "
               "각 줄은 직전 줄의 해시를 이어 SHA-256 으로 묶여 있어(해시 체인) 한 줄이라도 고치면 검사에 걸립니다.")
    if st.button("해시 체인 위변조 검사", icon=":material/verified:"):
        v = ui.load(api.get, "/admin/audit/verify")
        if v["ok"]:
            st.success(f"정상 · {v['checked']}줄 모두 해시 일치 · 마지막 해시 {v['head'][:16]}…", icon=":material/verified:")
        else:
            st.error(f"위변조 의심 · {v['broken_at']} 에서 끊김 — {v['reason']}", icon=":material/gpp_bad:")
    if not rows:
        st.info("조건에 맞는 이력이 없습니다.")
        return
    # 12열 → 9열: 이전 · 이후 상태는 '운행중 → 하차완료' 한 칸(한글), 해시는 현재 해시 앞 8자리
    # (감사 번호 · 이전 해시는 칸에 마우스를 올리면 툴팁으로) · 값이 없으면 '-'
    c = st.column_config.TextColumn
    st.dataframe(pd.DataFrame([{
        "일시": ui.dt_text(e["at"], "%m.%d %H:%M:%S"), "화물 ID": e["order_id"] or "-",
        "구분": "재무" if e["financial"] else ("청구" if e["kind"] == "billing" else "화물"),
        "이벤트": e["label"], "상태 변화": f'{ui.state_text(e["from"])} → {ui.state_text(e["to"])}',
        "누가": e["actor"] or "-", "금액": ui.won(e["amount"]), "비고": e["note"] or "-",
        "해시": (e.get("current_hash") or "-")[:8],
    } for e in rows]), hide_index=True, width="stretch", height=ui.table_height(len(rows)), column_config={
        "일시": c(width=105), "화물 ID": c(width=130), "구분": c(width=45), "이벤트": c(width=140),
        "상태 변화": c(width=125), "누가": c(width=115), "금액": c(width=70),
        "비고": c(width=125, help="칸에 마우스를 올리면 전체 비고"),
        "해시": c(width=75, help="이 줄의 SHA-256 해시 앞 8자리 (직전 줄 해시를 이어 계산)")})


# ------------------------------------------------------------------ 결제 내역 · 취소
def _payments_tab(payments: list[dict]):
    st.caption("모의 PG: 실제 결제사 · 카드 · 계좌를 쓰지 않습니다. 거래번호와 승인번호는 모의 값입니다.")
    if not payments:
        st.info("결제 내역이 없습니다.")
        return
    df = pd.DataFrame([{
        "결제일": ui.dt_text(p["approved_at"], "%m.%d %H:%M"), "화물 ID": p["order_id"], "화물": p["title"],
        "화주": p["shipper"], "차주": p["driver"], "수단": p["method_label"], "거래번호": p["tid"],
        "금액": p["amount"], "상태": "승인" if p["status"] == "APPROVED" else "취소",
    } for p in payments])
    c = st.column_config.TextColumn
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="pay_table", height=ui.table_height(len(df)), column_config={
                          "결제일": c(width=85), "화물 ID": c(width=130), "화물": c(width=135), "화주": c(width=95),
                          "차주": c(width=60), "수단": c(width=105), "거래번호": c(width=130),
                          "금액": st.column_config.NumberColumn(format="%,d원", width=90), "상태": c(width=50)})
    sel = ev.selection.rows
    if not sel or sel[0] >= len(payments):
        st.caption("행을 선택하면 결제를 취소(환불)할 수 있습니다.")
        return
    p = payments[sel[0]]
    if p["status"] != "APPROVED":
        st.caption(f"이미 취소된 결제입니다 · {p['cancel_reason']} · {p['cancelled_at']}")
        return
    with st.container(border=True):
        st.write(f"**{p['order_id']} · {p['title']}** — {ui.won(p['amount'])} · {p['method_label']} · {p['tid']}")
        reason = st.text_input("취소 사유", value="운임 정정 요청", max_chars=100)
        if st.button("결제 취소 (모의 PG 환불)", type="primary", icon=":material/undo:"):
            if ui.act(api.post, f"/admin/payments/{p['order_id']}/refund", json={"reason": reason},
                      success=f"{p['order_id']} 결제를 취소했습니다. 차주가 다시 청구할 수 있습니다."):
                st.rerun()
