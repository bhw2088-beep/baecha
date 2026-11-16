"""
관리자 컴플라이언스 (Phase 7): 차주 가입 승인 · 감사 로그(FSM 상태 전이 · 재무 변경) · 모의 PG 결제 내역/취소.
"""

import pandas as pd
import streamlit as st

import api
import ui

EVENT_TONE = {"create": "gray", "assign": "brand", "start": "blue", "deliver": "teal", "request": "amber",
              "pay": "green", "settle": "green", "refund": "red", "unsettle": "red"}


def page():
    ui.page_header("CONTROL CENTER", "컴플라이언스",
                   "차주 가입 서류를 심사하고, 모든 상태 변경 · 재무 변경 이력과 결제 내역을 확인합니다.")
    drivers = ui.load(api.get, "/admin/compliance/drivers")
    pending = [d for d in drivers if d["status"] == "PENDING"]
    payments = ui.load(api.get, "/admin/payments")
    audit = ui.load(api.get, "/admin/audit", limit=500)
    ui.kpis([
        {"label": "가입 승인 대기", "value": len(pending), "unit": "명", "alert": bool(pending),
         "note": "승인 전에는 배차를 받을 수 없음"},
        {"label": "감사 로그", "value": len(audit), "unit": "건", "note": "최근 500건 · 추가만 가능"},
        {"label": "재무 변경", "value": sum(1 for e in audit if e["financial"]), "unit": "건",
         "note": "청구 · 결제 · 취소"},
        {"label": "결제 승인 (모의 PG)", "value": ui.won(sum(p["amount"] for p in payments if p["status"] == "APPROVED")),
         "note": f"취소 {sum(1 for p in payments if p['status'] == 'CANCELLED')}건"},
    ])
    t1, t2, t3 = st.tabs([f"차주 가입 승인 · {len(pending)}", "감사 로그 (Audit Trail)", "결제 내역 · 취소 (모의 PG)"])
    with t1:
        _drivers_tab(drivers)
    with t2:
        _audit_tab(audit)
    with t3:
        _payments_tab(payments)


# ------------------------------------------------------------------ 가입 승인
def _drivers_tab(drivers: list[dict]):
    pending = [d for d in drivers if d["status"] == "PENDING"]
    if not pending:
        st.info("승인 대기 중인 가입 신청이 없습니다. 회원가입 화면에서 차주로 가입하면 여기에 나타납니다.",
                icon=":material/verified_user:")
    for d in pending:
        with st.container(border=True):
            docs = " · ".join(f"{'운전면허증' if x['kind'] == 'license' else '차량 사진'} {x['filename']} "
                              f"({x['size'] / 1024:.0f}KB)" for x in d["documents"]) or "제출 서류 없음"
            ui.html(f'<div class="ct-row"><div><div class="ct-strong" style="font-size:16px">{d["name"]} · '
                    f'{d["truck_type"]} · {d["plate"]}</div><div class="ct-muted">{d["id"]} · {d["email"] or "-"} · '
                    f'{d["phone"] or "-"} · 대기 지역 {d["location"]} · 사업자 {d.get("biz_no") or "-"}</div>'
                    f'<div class="ct-muted" style="margin-top:6px">제출 서류: {docs}</div></div>'
                    f'<div>{ui.pill("승인 대기", "amber")}</div></div>')
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
    st.dataframe(pd.DataFrame([{
        "기사": d["name"], "ID": d["id"], "차종": d["truck_type"], "차량": d["plate"], "대기 지역": d["location"],
        "심사": {"PENDING": "승인 대기", "APPROVED": "승인", "REJECTED": "반려"}.get(d["status"], d["status"]),
        "처리": d.get("decided_by") or "-", "사유": d.get("reason") or "",
    } for d in drivers]), hide_index=True, width="stretch")


# ------------------------------------------------------------------ 감사 로그
def _audit_tab(audit: list[dict]):
    c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
    q = c1.text_input("화물 ID 검색", placeholder="예: ORD-20261116-01", label_visibility="collapsed")
    fin = c2.toggle("재무 변경만 (청구 · 결제 · 취소)")
    rows = [e for e in audit if (not q or q.strip().upper() in e["order_id"]) and (not fin or e["financial"])]
    st.caption("상태는 engines/fsm/order_fsm.py 의 전이표로만 바뀌고, 모든 전이가 여기에 남습니다. 수정 · 삭제할 수 없습니다.")
    if not rows:
        st.info("조건에 맞는 이력이 없습니다.")
        return
    st.dataframe(pd.DataFrame([{
        "번호": e["id"], "일시": ui.dt_text(e["at"], "%m.%d %H:%M:%S"), "화물 ID": e["order_id"],
        "구분": "재무" if e["financial"] else ("청구" if e["kind"] == "billing" else "화물"),
        "이벤트": e["label"], "이전": e["from"] or "-", "이후": e["to"], "누가": e["actor"],
        "금액": e["amount"], "비고": e["note"],
    } for e in rows]), hide_index=True, width="stretch", height=480, column_config={
        "금액": st.column_config.NumberColumn(format="%,d원"),
        "비고": st.column_config.TextColumn(width="large")})


# ------------------------------------------------------------------ 결제 내역 · 취소
def _payments_tab(payments: list[dict]):
    st.caption("모의 PG: 실제 결제사 · 카드 · 계좌를 쓰지 않습니다. 거래번호와 승인번호는 모의 값입니다.")
    if not payments:
        st.info("결제 내역이 없습니다.")
        return
    df = pd.DataFrame([{
        "결제일": p["approved_at"][:16].replace("T", " "), "화물 ID": p["order_id"], "화물": p["title"],
        "화주": p["shipper"], "차주": p["driver"], "수단": p["method_label"], "거래번호": p["tid"],
        "금액": p["amount"], "상태": "승인" if p["status"] == "APPROVED" else "취소",
    } for p in payments])
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="pay_table", column_config={"금액": st.column_config.NumberColumn(format="%,d원")})
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
