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
    # ---- 외부 연동 (없으면 모의 · 규칙 기반으로 동작) ----
    nts_api_key: str = ""                         # 국세청 사업자 진위확인 (data.go.kr 인증키 Decoding)
    gemini_api_key: str = ""                      # Google Gemini (AI 관제 자연어 명령)
    gemini_model: str = "gemini-3.8-flash"      # 2.5-flash 는 신규 키에서 404 (2026-11 확인)
    # ---- 엔진 기준값 ----
    ml_threshold: float = 0.75                    # LightGBM 수락 예측 보수적 임계값 (미만이면 자동 제안 안 함)
    safety_margin: float = 0.05                   # 적재 안전율: 부피 · 중량 95% 초과 시 차단 (90% 초과 경고)
    top_load_margin: float = 0.10                 # 상부 허용하중 안전 여유: 상자 위에는 허용하중의 90% 까지만 (신고 무게 오차 흡수)
    deviation_km: float = 1.0                     # 경로 이탈 판정 거리 (계획 경로에서 1km 이상)
    deviation_hits: int = 3                       # 연속 몇 번 벗어나야 경고할지 (디바운싱)
    delay_alert_min: int = 30                     # 계획 ETA 대비 지연 알림 기준 (분)
    # ---- 시연 ----
    demo_auto_deliver: bool = False               # 데모: 도착(진행률 97%)한 운행을 자동 하차 완료 → 기사가 하차지에서 다음 배차 대기


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
ML_THRESHOLD = settings.ml_threshold  # 수락 예측이 이 값 미만인 기사에게는 자동 제안을 보내지 않는다
SETTING_LABEL = {"radius_km": ("탐색 반경", "km"), "timeout_sec": ("수락 제한시간", "초"), "w_dist": ("거리 가중치", "")}
