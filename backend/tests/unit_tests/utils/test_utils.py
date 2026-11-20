"""공용 함수 (utils: errors · security · redis_client · perf) 단위 테스트."""

import asyncio

import pytest
from fastapi import HTTPException

from src.utils import perf, redis_client, security
from src.utils.errors import ApiError


# ---------------------------------------------------------------- ApiError
def test_ctor_api_error():
    e = ApiError(404, "없음")
    assert (e.status, e.detail) == (404, "없음") and isinstance(e, Exception)


# ---------------------------------------------------------------- 비밀번호 해시
def test_hash_pw_smoke():
    h = security._hash_pw("password1", "salt")
    assert isinstance(h, str) and len(h) == 64


def test_hash_pw_actual_deterministic_and_salted():
    assert security._hash_pw("password1", "a") == security._hash_pw("password1", "a")
    assert security._hash_pw("password1", "a") != security._hash_pw("password1", "b")
    assert security._hash_pw("password1", "a") != security._hash_pw("password2", "a")


# ---------------------------------------------------------------- 역할 체크
def test_require_actual_role_mismatch_is_403():
    checker = security.require("admin")
    with pytest.raises(HTTPException) as ex:
        asyncio.run(checker(user={"role": "shipper"}))
    assert ex.value.status_code == 403
    assert asyncio.run(checker(user={"role": "admin"})) == {"role": "admin"}


def test_current_user_actual_missing_token_is_401(monkeypatch):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(security.current_user(authorization="", _=None))
    assert ex.value.status_code == 401


# ---------------------------------------------------------------- Redis 없을 때 (메모리 모드)
def test_redis_status_smoke_without_server():
    st = redis_client.redis_status()
    assert "ok" in st
    if not st["ok"]:
        assert redis_client.available() is False


# ---------------------------------------------------------------- perf (성능 기록)
def test_perf_record_and_snapshot_actual():
    perf.record("request", perf.SLOW_REQUEST_MS + 1, path="/test")
    snap = perf.snapshot()
    assert isinstance(snap, dict)
