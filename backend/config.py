"""
서버 설정 (pydantic-settings).

값은 환경변수 → 프로젝트 루트의 .env → 아래 기본값 순서로 읽는다.
  - 로컬 실행: .env 의 localhost 주소 사용
  - 도커 실행: docker-compose.yml 이 DATABASE_URL · REDIS_URL 을 컨테이너 주소(db, redis)로 덮어씀
"""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 수정본 폴더


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.path.join(ROOT, ".env"), env_file_encoding="utf-8",
                                      extra="ignore")

    database_url: str = "postgresql+psycopg://baecha:baecha@localhost:5432/baecha"
    redis_url: str = "redis://localhost:6379/0"
    admin_approval_code: str = "CT-ADMIN-2026"   # 관제자 회원가입 승인 코드
    time_scale: int = 20                          # 시연용 배속: 실제 1초 = 운행 20초
    sim_api_key: str = "sim-dev-key"              # 가상 기사 시뮬레이터 전용 내부 API 키 (X-Sim-Key)


settings = Settings()

# 기존 코드가 쓰던 이름 (services 에서 import)
TIME_SCALE = settings.time_scale
ADMIN_APPROVAL_CODE = settings.admin_approval_code

# ---- 자동 매칭 설정 기본값 (관제 화면에서 변경 가능 · 서버 재시작 시 기본값으로) ----
DEFAULT_SETTINGS = {
    "radius_km": 50,     # 상차지 탐색 반경 (도로거리)
    "timeout_sec": 60,   # 기사 수락 제한시간
    "w_dist": 0.6,       # 점수 가중치: 거리 (적재율 = 1 − 거리)
}
ML_WEIGHT = 0.3  # 최종 점수 = 규칙 점수(거리 · 적재율) 70% + LightGBM 수락 예측 30%
SETTING_LABEL = {"radius_km": ("탐색 반경", "km"), "timeout_sec": ("수락 제한시간", "초"), "w_dist": ("거리 가중치", "")}
