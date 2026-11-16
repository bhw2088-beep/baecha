"""화주 · 차주 공용: 운송 완료 화물 상호 평가(별점 + 태그) 패널."""

import streamlit as st

import api
import ui


def pending_reviews_panel(role: str):
    """아직 평가하지 않은 완료 화물 → 별점 · 태그 · 코멘트 남기기. role: shipper(→기사) · driver(→화주)."""
    try:
        pending = api.get("/reviews/pending")
        tags = api.get("/reviews/tags")
        mine = api.get("/reviews/mine")
    except api.ApiError as e:
        st.error(str(e))
        return
    r = mine.get("rating") or {}
    who = "기사" if role == "shipper" else "화주"
    avg = f"{r['avg']:.2f}★ ({r['count']}건)" if r.get("avg") is not None else "아직 받은 평가 없음"
    head = f"상호 평가 · 평가할 운송 {len(pending)}건 · 내가 받은 평점 {avg}"
    with st.expander(head, icon=":material/star_rate:", expanded=bool(pending)):  # 평가할 게 있으면 펼쳐 둔다
        st.caption(f"운송이 끝난 화물의 {who}를 평가해 주세요. 기사 평점은 자동 매칭 AI에 반영되고, "
                   f"{tags['blacklist_rule']}.")
        if r.get("top_tags"):
            ui.html('<div>' + " ".join(ui.pill(t, "gray") for t in r["top_tags"]) + '</div>')
        if not pending:
            st.info("평가할 운송이 없습니다.")
            return
        pick = st.selectbox("평가할 운송", pending, key=f"rv_pick_{role}",
                            format_func=lambda p: f"{p['order_id']} · {p['title']} · {who} {p['other']}")
        options = tags["driver" if role == "shipper" else "shipper"]
        with st.form(f"review_{role}", border=False):
            c1, c2 = st.columns([1, 3])
            stars = c1.select_slider("별점", options=[1, 2, 3, 4, 5], value=5, format_func=lambda s: "★" * s)
            good = c2.multiselect("좋았던 점", options["good"])
            bad = c2.multiselect("아쉬운 점", options["bad"])
            comment = st.text_input("한 줄 코멘트 (선택)", max_chars=200)
            if st.form_submit_button("평가 등록", type="primary", icon=":material/rate_review:"):
                if ui.act(api.post, "/reviews", json={"order_id": pick["order_id"], "stars": stars,
                                                      "tags": good + bad, "comment": comment},
                          success=f"{pick['order_id']} 평가를 등록했습니다."):
                    st.rerun()
