"""
FastAPI 백엔드 호출 클라이언트. 화면 코드는 requests 를 직접 쓰지 않고 이 모듈만 사용한다.
서버 주소는 환경변수 BAECHA_API_BASE 로 바꿀 수 있다.
"""

import os

import requests
import streamlit as st

API_BASE = os.getenv("BAECHA_API_BASE", "http://127.0.0.1:8000/api/v1")
TIMEOUT = 5


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, *, raw: bool = False, timeout: float = TIMEOUT, **kwargs):
    """timeout: 기본 5초. AI 관제 실장처럼 외부 AI 응답을 기다리는 호출만 길게 준다."""
    headers = kwargs.pop("headers", {})
    token = st.session_state.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=timeout, **kwargs)
    except requests.Timeout:
        raise ApiError(f"응답이 {timeout:.0f}초 안에 오지 않았습니다. 잠시 후 다시 시도해 주세요.")
    except requests.RequestException:
        raise ApiError(f"API 서버({API_BASE})에 연결할 수 없습니다. 백엔드가 실행 중인지 확인해 주세요.")
    if resp.status_code == 401 and token:
        # 서버 재시작 등으로 토큰이 만료됨 → 로그인 화면으로
        st.session_state.pop("token", None)
        st.session_state.pop("user", None)
        st.session_state._cookie_clear = True   # 쿠키도 지움 (session.flush 에서 반영)
        st.session_state._skip_restore = True
        raise ApiError("세션이 만료되었습니다. 다시 로그인해 주세요.", 401)
    if not resp.ok:
        try:
            detail = resp.json().get("detail")
            if isinstance(detail, list):  # Pydantic 검증 오류
                detail = " / ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail)
        except ValueError:
            detail = resp.text
        raise ApiError(detail or f"요청 실패 ({resp.status_code})", resp.status_code)
    return resp.content if raw else resp.json()


def get(path: str, **params):
    return _request("GET", path, params={k: v for k, v in params.items() if v is not None})


def get_bytes(path: str, **params) -> bytes:
    return _request("GET", path, raw=True, params=params)


def post(path: str, json: dict | None = None, **kwargs):
    return _request("POST", path, json=json, **kwargs)


def put(path: str, json: dict | None = None, **kwargs):
    return _request("PUT", path, json=json, **kwargs)
