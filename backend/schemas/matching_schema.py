"""헝가리안 배차 메트릭스 · GPS 핑 스키마."""

from pydantic import BaseModel, Field


class PingIn(BaseModel):
    """실시간 GPS 핑 (POST /tracking/ping). 기사 앱은 토큰으로, 가상 기사는 X-Sim-Key + driver_id 로 보낸다."""
    order_id: str
    lat: float = Field(ge=33.0, le=39.0)    # 대한민국 범위
    lon: float = Field(ge=124.0, le=132.0)
    speed: float = Field(0, ge=0, le=200)
    ts: str | None = None
    driver_id: str | None = None           # 시뮬레이터 전용 (기사 앱은 토큰의 기사 ID 사용)


class PingBatchIn(BaseModel):
    pings: list[PingIn] = Field(max_length=500)


class RoundLogOut(BaseModel):
    """matching_metrics_logs 한 줄 (배정 라운드)."""
    id: str
    at: str
    orders: int
    drivers: int
    assigned: int
    greedy_assigned: int
    latency_ms: float
    hungarian_avg_km: float | None
    greedy_avg_km: float | None
    deadhead_reduction_pct: float | None
    threshold_filtered: int
    failed: int


class MatchingMetricsOut(BaseModel):
    """실시간 매칭 메트릭스 (관제 '매칭 분석' · 벤치마킹 보고서)."""
    rounds: int
    dispatch_latency_p95_sec: float | None
    dispatch_latency_p95_ms: float | None
    target_p95_sec: float
    empty_deadhead_km_reduction_pct: float | None
    rejection_rate_pct: float | None
    threshold_filtered: int
    ml_threshold: float
    recent: list[RoundLogOut]


class AskIn(BaseModel):
    """AI 관제 자연어 명령."""
    text: str = Field(min_length=1, max_length=500)
