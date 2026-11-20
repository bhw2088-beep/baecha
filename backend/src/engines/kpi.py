"""
구현 단계별 현실적 KPI 목표치 (설계 기준 · 스트레스 테스트 판정 · 벤치마킹 보고서 · GET /admin/kpi 공용, 화면에는 표시하지 않음)

업계 표준(Standard / Legacy) 대비 이 시스템이 목표로 하는 '현실적' 수치를 한 곳에 정의한다.
처음부터 극단적 성능(예: 0.1초 미만)을 노리지 않고, 아래 목표를 기준선(마커)으로 잡아 시뮬레이터로 검증한다.
판정은 순수 함수(judge)라 실시간 값 · 스트레스 테스트 값 어디에나 같은 잣대로 쓸 수 있다.

status: pass(달성) · warn(근접 · 목표 범위 안) · fail(미달) · none(측정 전)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Kpi:
    key: str
    area: str
    name: str
    standard: str              # 업계 표준 (Standard / Legacy)
    target: str                # 구현 단계 현실적 목표
    unit: str
    judge: Callable[[float], str]
    strategy: str              # 달성 전략 (이 시스템에서 무엇으로)
    digits: int = 1


def _below(limit: float, warn: float | None = None):
    return lambda v: "pass" if v < limit else ("warn" if warn is not None and v < warn else "fail")


def _above(limit: float, warn: float | None = None):
    return lambda v: "pass" if v >= limit else ("warn" if warn is not None and v >= warn else "fail")


KPIS: tuple[Kpi, ...] = (
    Kpi("compliance_auto_pct", "컴플라이언스 & 보안", "사업자 · 자격 가입 검증 자동화율",
        "수기 검증 (오류 발생 가능)", "100% (자동화 규칙)", "%", _above(100.0),
        "사업자번호 Modulus 10 · 사업용 노란 번호판(COMMERCIAL_YELLOW) · 필수 서류 자동 판정", 0),
    Kpi("packing_violation_pct", "적재 및 안전", "물리적 과적 및 적재 오탐 발생률",
        "현장 경험 의존 (5~10% 오차)", "0% (룰 엔진 강제 차단)", "%", _below(0.0001),
        "5% 보수적 안전율 + 적재 규칙 R0~R10 + Extreme Point", 1),
    Kpi("dispatch_p95_sec", "매칭 & 배차 효율", "배차 지연 시간 (P95)",
        "수동 · 전화 배차 (수십 분 소요)", "< 3.0초", "초", _below(3.0),
        "헝가리안 마이크로 배치 (대기 화물 × 공차 한 번에 배정)", 2),
    Kpi("rejection_rate_pct", "매칭 & 배차 효율", "배차 거절률 (Rejection Rate)",
        "25 ~ 30% 이상", "< 15 ~ 20%", "%", _below(15.0, 20.0),
        "LightGBM 수락 예측 (lgb_score ≥ 0.75 만 제안) + 수급 서지 프라이싱", 1),
    Kpi("deadhead_vs_nearest_pct", "매칭 & 배차 효율", "공차 거리 단축률 (단순 최단거리 매칭 대비)",
        "기준 없음 (빈 차 이동 잦음)", "기준선 대비 10 ~ 15% 단축", "%", _above(10.0, 0.0),
        "헝가리안 전체 최적 배정 + 권역 선호도(cf_score) 반영", 1),
    Kpi("gps_false_alarm_pct", "실시간 관제", "경로 이탈 오알람률 (False Positive)",
        "GPS 튀김으로 빈번한 경고 발생", "< 5%", "%", _below(5.0),
        "3회 연속 디바운싱으로 단발 튐(노이즈) 필터링", 1),
    Kpi("relay_p95_sec", "실시간 관제", "관제 반영 지연 (GPS 핑 → 관제 데이터, P95)",
        "일반 웹 새로고침형 (2 ~ 3초)", "< 2.0초", "초", _below(2.0),
        "Redis Streams 파이프라인 (XADD → 소비자 그룹 → 즉시 반영)", 2),
    Kpi("pg_success_pct", "비즈니스 정산", "PG 정산 및 에스크로 성공률",
        "95 ~ 98%", "> 99.0%", "%", _above(99.0),
        "인수증(DELIVERED) 기반 자동 청구 · 승인 연동 (현재 모의 PG)", 1),
)

BY_KEY = {k.key: k for k in KPIS}
STATUS_LABEL = {"pass": "달성", "warn": "근접", "fail": "미달", "none": "측정 전"}


def evaluate(key: str, value: float | None, *, source: str, basis: str = "") -> dict:
    """KPI 한 줄: 정의 + 현재 값 + 판정. source = '실시간' | '스트레스 테스트' 등."""
    k = BY_KEY[key]
    status = "none" if value is None else k.judge(value)
    shown = "-" if value is None else f"{value:.{k.digits}f}{k.unit}"
    return {"key": k.key, "area": k.area, "name": k.name, "standard": k.standard, "target": k.target,
            "value": value, "display": shown, "unit": k.unit, "status": status, "status_label": STATUS_LABEL[status],
            "source": source, "basis": basis, "strategy": k.strategy}


def summary(rows: list[dict]) -> dict:
    measured = [r for r in rows if r["status"] != "none"]
    return {"measured": len(measured), "total": len(rows),
            "pass": sum(1 for r in measured if r["status"] == "pass"),
            "warn": sum(1 for r in measured if r["status"] == "warn"),
            "fail": sum(1 for r in measured if r["status"] == "fail")}
