"""인증 요청 스키마."""

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str
    password: str


class DecisionIn(BaseModel):
    """관제 가입 심사 결정."""
    approve: bool
    reason: str = Field("", max_length=200)
