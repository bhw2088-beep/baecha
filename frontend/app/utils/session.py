"""
로그인 유지 (새로고침 F5 해도 로그아웃되지 않게).

Streamlit 은 새로고침하면 세션(st.session_state)이 새로 만들어져 로그인 정보가 사라진다.
그래서 로그인 토큰을 브라우저 쿠키에 저장해 두고, 새 세션이 시작될 때 쿠키의 토큰으로
백엔드(/auth/me)에 확인한 뒤 로그인 상태를 복원한다.

- 토큰을 URL 에 넣지 않는다 (주소창·방문 기록에 남지 않도록)
- 쿠키는 12시간 유지, 로그아웃하거나 토큰이 무효(백엔드 재시작 등)면 삭제
"""

import json

import streamlit as st

from utils import api

COOKIE = "ct_token"
MAX_AGE = 60 * 60 * 12  # 12시간


def _run_js(code: str):
    st.html(f"<script>{code}</script>", unsafe_allow_javascript=True)


def remember(token: str):
    """로그인 직후 호출: 다음 화면 렌더링 때 쿠키에 토큰을 저장한다."""
    st.session_state._cookie_set = token


def forget():
    """로그아웃·세션 만료 때 호출: 쿠키를 지우고, 이번 세션에서는 쿠키로 자동 복원하지 않는다."""
    st.session_state._cookie_clear = True
    st.session_state._skip_restore = True  # st.context.cookies 는 세션 시작 시점 값이라 지워도 남아 있음


def restore():
    """새 세션(새로고침)이면 쿠키의 토큰으로 로그인 복원."""
    if st.session_state.get("user") or st.session_state.get("_skip_restore"):
        return
    st.session_state._skip_restore = True  # 세션당 한 번만 시도
    token = st.context.cookies.get(COOKIE)
    if not token:
        return
    st.session_state.token = token
    try:
        st.session_state.user = api.get("/auth/me")
    except api.ApiError:
        st.session_state.pop("token", None)
        st.session_state.pop("user", None)
        st.session_state._cookie_clear = True


def flush():
    """쌓인 쿠키 작업을 브라우저에 반영 (매 렌더링마다 호출)."""
    token = st.session_state.pop("_cookie_set", None)
    if token:
        _run_js(f"document.cookie = '{COOKIE}=' + {json.dumps(token)} + "
                f"'; path=/; max-age={MAX_AGE}; SameSite=Lax';")
    if st.session_state.pop("_cookie_clear", None):
        _run_js(f"document.cookie = '{COOKIE}=; path=/; max-age=0; SameSite=Lax';")
