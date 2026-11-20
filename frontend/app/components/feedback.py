"""알림 · API 호출 래퍼: 토스트(flash) · 실패 시 오류 표시(load / act)."""

import streamlit as st

from utils import api


def flash(message: str, icon: str = "✅"):
    """다음 렌더링에서 토스트로 보여줄 메시지 (st.rerun 이후에도 유지)."""
    st.session_state.setdefault("_flash", []).append((message, icon))


def show_flash():
    for message, icon in st.session_state.pop("_flash", []):
        st.toast(message, icon=icon)


def load(fn, *args, **kwargs):
    """API 호출 실패 시 화면에 오류를 보여주고 렌더링을 멈춘다."""
    try:
        return fn(*args, **kwargs)
    except api.ApiError as e:
        if e.status == 401:
            st.rerun()
        st.error(str(e), icon="⚠️")
        st.stop()


def act(fn, *args, success: str | None = None, **kwargs):
    """버튼 액션용: 성공하면 flash + True, 실패하면 오류 표시 + False."""
    try:
        result = fn(*args, **kwargs)
    except api.ApiError as e:
        st.error(str(e), icon="⚠️")
        return None
    if success:
        flash(success)
    return result if result is not None else True
