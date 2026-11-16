"""
배차의 민족 웹 (Streamlit)

실행 (프로젝트 루트에서, 백엔드 먼저):
    uvicorn backend.main:app --reload --reload-dir backend --port 8000
    streamlit run frontend/app.py
"""

import importlib

import streamlit as st

import api
import nav
import session
import ui

# pages/ 의 화면 모듈 (파일명이 숫자로 시작해 importlib 로 가져온다).
# st.navigation 을 쓰므로 Streamlit 의 pages/ 자동 메뉴는 생기지 않는다.
auth = importlib.import_module("pages.0_auth")
shipper = importlib.import_module("pages.1_shipper_portal")
admin = importlib.import_module("pages.2_control_tower")
compliance = importlib.import_module("pages.3_admin_compliance")
driver = importlib.import_module("pages.4_driver_app")

st.set_page_config(page_title="배차의 민족", page_icon="🚚", layout="wide", initial_sidebar_state="auto")  # auto: PC 는 펼침, 휴대폰은 접힌 채로 시작
ui.inject_css()
ui.show_flash()
session.restore()  # 새로고침(F5) 시 쿠키의 토큰으로 로그인 복원

ROLE_LABEL = {"shipper": "화주", "driver": "차주", "admin": "통합관제"}

PAGES = {
    "shipper": [
        st.Page(shipper.dispatch, title="배차 · 실시간 관제", icon=":material/local_shipping:", url_path="dispatch", default=True),
        st.Page(shipper.new_order_page, title="신규 화물 등록", icon=":material/view_in_ar:", url_path="new-order"),
        st.Page(shipper.history, title="정산 · 운송 이력", icon=":material/receipt_long:", url_path="history"),
    ],
    "driver": [
        st.Page(driver.board, title="배차 제안 · 내 운행", icon=":material/notifications_active:", url_path="board",
                default=True),
        st.Page(driver.earnings, title="수익 · 화주 청구", icon=":material/payments:", url_path="earnings"),
    ],
    "admin": [
        st.Page(admin.control_map, title="전국 관제 지도", icon=":material/map:", url_path="map", default=True),
        st.Page(admin.matching, title="매칭 분석", icon=":material/monitoring:", url_path="matching"),
        st.Page(admin.anomalies, title="이상 감지", icon=":material/emergency_home:", url_path="anomalies"),
        st.Page(compliance.page, title="컴플라이언스", icon=":material/verified_user:", url_path="compliance"),
    ],
}


def sidebar(user: dict):
    with st.sidebar:
        st.html(f'<div class="ct" style="padding:2px 0 4px">{ui.LOGO}</div>')
        initial = (user["name"] or "?")[0]
        st.html(f'<div class="ct"><div class="ct-user">{ui.pill(ROLE_LABEL[user["role"]], "brand")}'
                f'<div class="top"><div class="ct-avatar">{initial}</div><div style="min-width:0">'
                f'<div class="n">{user["name"]}</div><div class="s">{user["sub"]}</div></div></div>'
                f'<div class="s" style="margin-top:10px">{user["email"]}</div></div></div>')
        if st.button("로그아웃", icon=":material/logout:", width="stretch"):
            try:
                api.post("/auth/logout")
            except api.ApiError:
                pass
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            session.forget()
            st.rerun()
        if user["role"] == "admin":
            if st.button("데모 데이터 초기화", icon=":material/restart_alt:", width="stretch",
                         help="DB 의 모든 화물 · 배차 · 정산 기록을 시연 시작 상태로 되돌립니다."):
                reset_dialog()
        st.caption(f"API · {api.API_BASE}")


@st.dialog("데모 데이터 초기화")
def reset_dialog():
    st.write("DB 에 저장된 **화물 · 배차 제안 · 운행 · 정산 · 이상 감지 · 매칭 설정**을 모두 지우고 "
             "시연 시작 상태(시드 데이터)로 되돌립니다.")
    st.warning("되돌릴 수 없습니다. 새로 가입한 계정과 등록한 화물도 사라집니다. 데모 계정 로그인은 유지돼요.",
               icon=":material/warning:")
    c1, c2 = st.columns(2)
    if c1.button("취소", width="stretch"):
        st.rerun()
    if c2.button("초기화", type="primary", width="stretch"):
        r = ui.load(api.post, "/admin/reset")
        ui.flash(f"데모 데이터로 초기화했습니다 · 화물 {r['orders']}건 · 기사 {r['drivers']}명 ({r['storage']})")
        st.rerun()


session.flush()  # 로그인/로그아웃에 따른 쿠키 저장·삭제
user = st.session_state.get("user")
if not user:
    st.html('<style>[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"]{display:none}</style>')
    # 로그인 전에는 어떤 주소(/dispatch 등)로 들어와도 로그인 화면을 보여준다 ("Page not found" 방지)
    deep_links = ["dispatch", "new-order", "history", "board", "earnings", "map", "matching", "anomalies", "compliance"]
    login_pages = [st.Page(auth.screen, title="로그인", icon=":material/login:", url_path="login", default=True)]
    login_pages += [st.Page(lambda: auth.screen(), title="로그인", url_path=path) for path in deep_links]
    st.navigation(login_pages, position="hidden").run()
else:
    sidebar(user)
    if user["role"] == "shipper":
        nav.PAGES.update(dispatch=PAGES["shipper"][0], new_order=PAGES["shipper"][1])
    st.navigation(PAGES[user["role"]], position="top").run()
