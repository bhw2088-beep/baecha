"""
보안 · 인증 공통: 비밀번호 해시, 토큰 인증, 역할 체크, 요청마다 시뮬레이션 1틱 진행. (원래 deps.py)

라우터와 의존성은 모두 async def 로 작성한다 → 이벤트 루프 한 스레드에서 순서대로 실행되므로
메모리 저장소에 락이 필요 없다. (DB 로 바꾸면 트랜잭션이 그 역할을 맡는다)
"""

import hashlib

from fastapi import Depends, Header, HTTPException


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def _store():
    # database 가 services(→ 이 모듈의 _hash_pw)를 import 하므로, 순환을 피하려고 사용할 때 가져온다
    from .database import store
    return store


async def tick_store() -> None:
    _store().tick()


async def current_user(authorization: str = Header(default=""), _=Depends(tick_store)) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    user = _store().user_by_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def require(role: str):
    async def checker(user: dict = Depends(current_user)) -> dict:
        if user["role"] != role:
            raise HTTPException(status_code=403, detail="이 메뉴에 접근할 권한이 없습니다.")
        return user
    return checker
