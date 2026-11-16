"""
Celery 앱 설정 (Redis 브로커 · 결과 저장)

무거운 계산(3D 빈패킹 시뮬레이션 등)을 API 서버가 아니라 별도 워커 컨테이너에서 처리한다.
  API ──(작업 등록)──▶ Redis db1 (브로커) ──▶ worker 컨테이너 ──(결과)──▶ Redis db2 ──▶ API 가 조회

워커 실행 (docker compose 의 worker 서비스):
    celery -A backend.workers.celery_app worker -l info --concurrency 2
"""

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
)

# celery -A backend.workers.celery_app 로 찾을 수 있게
app = celery_app
