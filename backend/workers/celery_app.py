"""
Celery 앱 설정 (Redis 브로커 · 결과 저장)

무거운 계산(3D 빈패킹 시뮬레이션 등)을 API 서버가 아니라 별도 워커 컨테이너에서 처리한다.
  API ──(작업 등록)──▶ Redis db1 (브로커) ──▶ worker 컨테이너 ──(결과)──▶ Redis db2 ──▶ API 가 조회

워커 실행 (docker compose 의 worker 서비스):
    celery -A backend.workers.celery_app worker -l info --concurrency 2
"""

import os

from celery import Celery

from ..config import settings


def _redis_db(url: str, db: int) -> str:
    base = url.rsplit("/", 1)[0] if url.count("/") >= 3 else url
    return f"{base}/{db}"


celery_app = Celery(
    "baecha",
    broker=_redis_db(settings.redis_url, 1),
    backend=_redis_db(settings.redis_url, 2),
    include=["backend.workers.packing_tasks"],
)
celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    timezone="Asia/Seoul", enable_utc=False,
    result_expires=3600,             # 결과는 1시간 보관
    task_time_limit=120,             # 작업 하나 최대 2분
    worker_prefetch_multiplier=1,    # 무거운 작업은 하나씩 가져가게
    task_track_started=True,
    # ---- 장애 복구: 워커가 작업 도중 죽어도 작업이 사라지지 않게 ----
    task_acks_late=True,             # 작업을 '받았을 때'가 아니라 '끝났을 때' 완료 표시 (ACK)
    task_reject_on_worker_lost=True,  # 워커 프로세스가 죽으면(강제 종료 · 메모리 부족) 작업을 큐로 되돌림
    # 완료 표시가 없는 작업을 다른 워커에게 다시 주기까지 기다리는 시간 (초) — 작업 최대 시간(120초)보다 길게
    # 되돌릴 작업 검사 주기: 기본은 10초 × 10회마다(약 100초) → 10초마다 (Redis 조회 1번이라 가벼움)
    broker_transport_options={"visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "300")),
                              "unacked_restore_interval": 10, "unacked_restore_throttle": 1},
)

# celery -A backend.workers.celery_app 로 찾을 수 있게
app = celery_app
