"""
AI 관제 실장 (AI Control Tower) — 관제자가 자연어로 현황을 묻고 요약을 받는다. 조회 · 요약 전용.

  POST /api/v1/ai/control-tower   {"command": "강남구 지연 상황 요약해줘", "history": [...]}
    → Gemini Function Calling (조회 함수만 · 전화번호 가림) · 키가 없거나 실패하면 규칙 기반 답
    → 설정 변경 · 조치 요청은 실행하지 않고 어느 화면에서 할 수 있는지 안내

엔진: engines/ai/gemini_agent.py (control_tower) · engines/ai/rule_agent.py (answer_readonly · region_status)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.config import settings
from src.orm.database import store
from src.utils.security import require

router = APIRouter(tags=["AI Control Tower"])
Admin = Depends(require("admin"))


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str = Field(max_length=4000)


class ControlTowerIn(BaseModel):
    """AI 관제 실장에게 보내는 질문 (이전 대화는 최근 6개까지만 씀)."""
    command: str = Field(min_length=1, max_length=500, examples=["강남구 지연 상황 요약해줘"])
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


@router.post("/ai/control-tower", dependencies=[Admin])
async def control_tower(body: ControlTowerIn):
    """AI 관제 실장: 현황 조회 · 요약 (설정을 바꾸지 않음). 응답: engine · answer · tools(쓴 조회 함수) · fallback."""
    from src.engines.ai import gemini_agent
    history = [t.model_dump() for t in body.history]
    # Gemini 응답(수 초)은 스레드에서 기다린다 → 그동안 다른 API 요청 · GPS 처리가 멈추지 않게
    return await asyncio.to_thread(gemini_agent.control_tower, store, settings, body.command, history)
