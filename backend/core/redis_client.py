"""
Redis 연결 · 상태 확인 · 분산 락 · Streams 공용 함수.

- REDIS_URL(.env · docker-compose)로 연결한다. 처음 쓸 때 만든다(지연 생성).
- Redis 가 없어도(로컬에서 도커 없이 실행 · 테스트) 서버는 계속 동작한다.
- 매칭 분산 락(Phase ⑤) · GPS Streams(Phase ⑦) · Celery 브로커(Phase ⑧)가 이 연결을 쓴다.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from ..config import settings

_client = None
_down_until = 0.0  # Redis 연결 실패 후 이 시각까지는 다시 시도하지 않음 (요청이 느려지지 않게)


def get_redis():
    """redis.Redis 클라이언트 (문자열로 주고받음)."""
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                       socket_connect_timeout=2, socket_timeout=5)
    return _client


def available() -> bool:
    """Redis 를 쓸 수 있는지 (실패하면 30초 동안은 바로 False)."""
    global _down_until
    if time.monotonic() < _down_until:
        return False
    try:
        get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        _down_until = time.monotonic() + 30
        return False


@contextmanager
def lock(name: str, timeout: float = 10, wait: float = 3):
    """Redis 분산 락: 여러 서버 · 워커가 같은 작업(매칭 배정 등)을 동시에 하지 않게 한다.
    with lock("matching") as got: ...  — got 이 False 면 다른 쪽이 실행 중.
    Redis 가 없으면(로컬 테스트) 락 없이 got=True 로 진행한다 (서버 1개일 때는 안전)."""
    if not available():
        yield True
        return
    lk = get_redis().lock(f"lock:{name}", timeout=timeout, blocking_timeout=wait)
    try:
        got = lk.acquire()
    except Exception:  # noqa: BLE001
        yield True
        return
    try:
        yield got
    finally:
        if got:
            try:
                lk.release()
            except Exception:  # noqa: BLE001 — 만료 등으로 이미 풀린 경우
                pass


def redis_status() -> dict:
    """Redis 연결 확인 (서버 시작 시 · /health 에서 사용)."""
    try:
        r = get_redis()
        r.ping()
        return {"ok": True, "version": r.info("server").get("redis_version")}
    except Exception as e:  # noqa: BLE001 — 연결 실패는 상태로만 알린다
        return {"ok": False, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"}
