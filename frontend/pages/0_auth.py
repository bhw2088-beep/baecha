"""로그인 / 회원가입 화면."""

import streamlit as st

import api
import session
import ui

ROLES = {"shipper": "화주 (기업)", "driver": "차주 (기사)", "admin": "통합관제"}
DEMO_PASSWORD = "baecha1234"


def _login(email: str, password: str):
    try:
        res = api.post("/auth/login", {"email": email, "password": password})
    except api.ApiError as e:
        st.error(str(e), icon="⚠️")
        return
    st.session_state.token = res["token"]
    session.remember(res["token"])
    st.session_state.user = res["user"]
    ui.flash(f"{res['user']['name']}님, 환영합니다.", "👋")
    st.rerun()


def _hero(meta: dict | None):
    s = (meta or {}).get("stats", {})
    st.html(f"""
    <div class="ct"><div class="ct-hero">
      <div style="display:flex;justify-content:space-between;align-items:center">
        {ui.LOGO}
        <div class="ct-live"><i></i>Live network</div>
      </div>
      <div>
        <h1>실을 수 있는 차,<br>가장 가까운 차를<br><em>자동으로.</em></h1>
        <p>화주가 품목을 등록하면 기사마다 3D 적재 시뮬레이션을 돌려, 전량 실리고 상차지에서 가까운 기사에게
           바로 배차를 제안합니다. 기사는 수락하고, 관제는 지켜봅니다.</p>
      </div>
      <div class="stats">
        <div><b>{s.get("vehicles", "-")}</b><span>네트워크 차량</span></div>
        <div><b>{s.get("moving", "-")}</b><span>지금 이동 중</span></div>
        <div><b>{s.get("orders_today", "-")}</b><span>오늘 등록 화물</span></div>
      </div>
    </div></div>""")


def screen():
    try:
        meta = api.get("/meta")
    except api.ApiError as e:
        meta = None
        st.error(str(e), icon="⚠️")

    left, gap, right = st.columns([1.15, 0.08, 1])
    with left:
        _hero(meta)
    with right:
        mode = st.session_state.get("auth_mode", "login")
        if mode == "login":
            _login_form(meta)
        else:
            _register_form(meta)


def _login_form(meta):
    st.html('<div class="ct" style="margin-top:24px"><div class="ct-eyebrow">Welcome back</div>'
            '<div class="ct-title">로그인</div><div class="ct-desc">등록된 계정으로 업무 공간에 접속하세요.</div></div>')
    with st.form("login", border=False):
        email = st.text_input("이메일", placeholder="name@company.com")
        password = st.text_input("비밀번호", type="password")
        if st.form_submit_button("로그인", type="primary", width="stretch"):
            if not email or not password:
                st.warning("이메일과 비밀번호를 입력해 주세요.")
            else:
                _login(email, password)

    st.html('<div class="ct" style="margin:18px 0 6px"><div class="ct-sec"><b>데모 계정으로 바로 체험</b>'
            f'<span>비밀번호 {DEMO_PASSWORD}</span></div></div>')
    demos = (meta or {}).get("demo_accounts", [])
    cols = st.columns(3)
    for col, acc in zip(cols, demos):
        with col:
            with st.container(border=True):
                st.html(f'<div class="ct">{ui.pill(ROLES[acc["role"]].split(" ")[0], "brand")}'
                        f'<div class="ct-strong" style="margin-top:8px">{acc["name"]}</div>'
                        # 부제목 줄 수가 달라도 세 카드 높이가 같도록 2줄 높이를 고정 (넘치면 … 처리)
                        f'<div class="ct-muted" style="font-size:12px;line-height:1.45;height:2.9em;overflow:hidden;'
                        f'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical" title="{acc["sub"]}">'
                        f'{acc["sub"]}</div></div>')
                if st.button("접속", key=f"demo_{acc['role']}", width="stretch"):
                    _login(acc["email"], DEMO_PASSWORD)

    st.write("")
    c1, c2 = st.columns([2, 1])
    c1.caption("아직 계정이 없으신가요?")
    if c2.button("회원가입", width="stretch"):
        st.session_state.auth_mode = "register"
        st.rerun()


def _register_form(meta):
    st.html('<div class="ct" style="margin-top:8px"><div class="ct-eyebrow">Get started</div>'
            '<div class="ct-title">파트너 계정 만들기</div>'
            '<div class="ct-desc">가입 유형에 따라 필요한 정보가 달라집니다.</div></div>')
    role = st.segmented_control("가입 유형", options=list(ROLES), format_func=ROLES.get, default="shipper",
                                key="reg_role") or "shipper"
    truck_types = (meta or {}).get("truck_types", ["11톤 냉동탑차"])

    with st.form("register", border=False):
        c1, c2 = st.columns(2)
        email = c1.text_input("이메일 (아이디)")
        name = c2.text_input("성명 / 담당자명")
        password = c1.text_input("비밀번호", type="password", help="8자 이상")
        password2 = c2.text_input("비밀번호 확인", type="password")
        phone = c1.text_input("연락처", placeholder="010-0000-0000")
        extra = {}
        files = {}
        if role == "shipper":
            extra["company"] = c2.text_input("기업 / 화주명")
            extra["biz_no"] = c1.text_input("사업자등록번호", placeholder="000-00-00000")
        elif role == "driver":
            extra["biz_no"] = c2.text_input("차주 사업자등록번호", placeholder="000-00-00000")
            extra["plate"] = c1.text_input("차량 번호", placeholder="경기 00바 0000")
            extra["truck_type"] = c2.selectbox("차종 / 톤수", truck_types)
            extra["base_location"] = c1.selectbox("주 대기 지역", (meta or {}).get("regions") or ["대전 중구"],
                                                  index=None, placeholder="지역 선택 · 입력해서 검색",
                                                  help="자동 매칭의 상차지 거리 계산 기준 위치")
            st.caption("신원 및 차량 증빙 (각 5MB 이하 이미지)")
            f1, f2 = st.columns(2)
            files["license_photo"] = f1.file_uploader("운전면허증 / 신분증", type=["png", "jpg", "jpeg"])
            files["vehicle_photo"] = f2.file_uploader("차량 번호판 / 차량 사진", type=["png", "jpg", "jpeg"])
        else:
            extra["approval_code"] = c2.text_input("관제센터 승인 코드", type="password",
                                                   help="관제센터 관리자에게 발급받은 보안 인증키")
        agree = st.checkbox("[필수] 이용약관 및 개인정보 처리방침에 동의합니다.")
        submitted = st.form_submit_button("가입하고 시작하기", type="primary", width="stretch")

    if submitted:
        if not agree:
            st.warning("약관에 동의해 주세요.")
        elif password != password2:
            st.warning("비밀번호가 일치하지 않습니다.")
        elif not email or not name:
            st.warning("이메일과 이름을 입력해 주세요.")
        else:
            form = {"role": role, "email": email, "password": password, "name": name, "phone": phone,
                    **{k: v for k, v in extra.items() if v}}
            upload = {k: (f.name, f.getvalue(), f.type) for k, f in files.items() if f is not None}
            try:
                res = api.post("/auth/register", data=form, files=upload or None)
            except api.ApiError as e:
                st.error(str(e), icon="⚠️")
            else:
                st.session_state.token = res["token"]
                session.remember(res["token"])
                st.session_state.user = res["user"]
                st.session_state.auth_mode = "login"
                ui.flash("가입이 완료되었습니다. 환영합니다!", "🎉")
                st.rerun()

    if st.button("← 로그인으로 돌아가기"):
        st.session_state.auth_mode = "login"
        st.rerun()
