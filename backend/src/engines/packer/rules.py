"""
적재 규칙표 (Rule) — '규칙 + Extreme Point' 적재 엔진의 단일 기준.

EP 엔진(engine.py)은 상자를 놓을 자리를 찾을 때 이 규칙들을 차례로 검사해, 하나라도 어기는 자리는 버린다.
검증기(validate.py)는 엔진과 별개로 '결과 좌표만 보고' 같은 규칙을 다시 채점한다.
→ 엔진 · 검증기 · 화면(제약 카드) · 미적재 사유가 모두 같은 규칙 번호(R0~R10)와 이름을 쓴다.

stage (규칙이 어디서 적용되는가)
  place   : 배치 중 차단 — 어기는 자리에는 아예 놓지 않음
  balance : 배치 중 선택 — 놓을 수 있는 자리가 여럿이면 규칙에 더 맞는 쪽을 고름 (+ 배치 후 판정)
  check   : 배치 후 판정만 (차량 선택 단계에서 이미 걸러짐)
"""

from __future__ import annotations

from dataclasses import dataclass

STAGE_LABEL = {
    "place": "배치 중 차단",
    "balance": "배치 중 선택 + 결과 판정",
    "check": "결과 판정",
}


@dataclass(frozen=True)
class Rule:
    code: str        # R0 ~ R10
    key: str         # 검증 결과 · 화면에서 쓰는 키 (기존 키 그대로)
    name: str        # 화면 표시 이름
    rule: str        # 규칙 설명 (고정 문구 · 수치가 들어가는 규칙은 검증기가 채움)
    stage: str       # place | balance | check
    unplaced: str = ""  # 이 규칙 때문에 못 실었을 때 미적재 사유


RULES: tuple[Rule, ...] = (
    Rule("R0", "compat", "차량-화물 호환", "냉장 화물은 냉장 차량에만", "check"),
    Rule("R1", "bounds", "적재함 경계", "모든 상자가 적재함 안쪽", "place", "남은 공간 부족"),
    Rule("R2", "overlap", "상자 간 비중첩", "부피가 겹치는 상자 없음", "place", "남은 공간 부족"),
    Rule("R3", "orientation", "방향 (세움 고정)", "세움 고정 품목은 높이축 유지 (수직축 90° 회전만)", "place"),
    Rule("R4", "weight", "최대 적재중량", "총 중량 ≤ 차량 최대 적재중량", "place", "최대 적재중량 초과"),
    Rule("R5", "support", "지지면 (안정성)", "바닥 면적의 일정 비율 이상이 아래 상자/바닥에 닿음", "place",
         "지지면 부족 (공중 부양 위험)"),
    Rule("R6", "stacking", "적층 · 상부 허용하중", "적층 불가 품목 위 적재 금지, 상부 하중 ≤ 허용하중의 90% (안전 여유)", "place",
         "아래 품목의 상부 허용하중 초과"),
    Rule("R7", "top_only", "최상단 배치 (파손주의 · 냉동)", "파손주의 · 냉동 표시 상자 위에는 아무것도 싣지 않음", "place",
         "아래가 파손주의 · 냉동(최상단 전용) 품목"),
    Rule("R8", "lifo", "하차 순서 (LIFO)", "먼저 내릴 화물의 위 · 뒷문 쪽을 나중에 내릴 화물이 막지 않음", "place",
         "하차 순서(LIFO) 위반"),
    Rule("R9", "safety", "보수적 안전율", "부피 · 중량 사용률을 (1 − 안전율) 이하로 — 포장 · 측정 오차 대비", "place",
         "안전율 한도 초과 (더 큰 차량 필요)"),
    Rule("R10", "cog", "축중량 균형 (산안법 제173조)",
         "좌우 편차 ≤ 10%(15% 초과 차단), 전후 무게중심 20~65%(15~75% 밖 차단)", "balance"),
)

BY_KEY: dict[str, Rule] = {r.key: r for r in RULES}

# 엔진 내부의 실패 사유 → (규칙 키, 문구를 따로 줄 때만 문구)
#   space = R1 경계 또는 R2 겹침 (자리가 없음)
FAIL_RULE: dict[str, tuple[str, str | None]] = {
    "space": ("bounds", None),
    "weight": ("weight", None),
    "support": ("support", None),
    "nostack": ("stacking", "아래가 적층 불가 품목"),
    "load": ("stacking", None),
    "top": ("top_only", None),
    "lifo": ("lifo", None),
    "safety": ("safety", None),
}


def unplaced_reason(fail_key: str) -> str:
    key, label = FAIL_RULE[fail_key]
    r = BY_KEY[key]
    return f"{r.code} {label or r.unplaced}"


def check_row(key: str, status: str, detail: str, violations: list, *, rule: str | None = None, **extra) -> dict:
    """검증 결과 한 줄 (화면 제약 카드 형식). 이름 · 번호 · 적용 단계는 규칙표에서."""
    r = BY_KEY[key]
    return {"key": key, "code": r.code, "name": r.name, "rule": rule or r.rule, "stage": STAGE_LABEL[r.stage],
            "status": status, "detail": detail, "violations": violations, **extra}


def describe() -> list[dict]:
    """규칙표 전체 (API · 문서용)."""
    return [{"code": r.code, "key": r.key, "name": r.name, "rule": r.rule, "stage": STAGE_LABEL[r.stage]}
            for r in RULES]
