"""
Redis 장애 복구 테스트 전용 Celery 작업 (tests/checks_6/2_redis_recovery.py 가 테스트 워커에만 -I 로 불러온다).
운영 워커(docker compose 의 worker)는 이 파일을 불러오지 않는다.
"""

import os
import time

from backend.workers.celery_app import celery_app


@celery_app.task(name="test.recovery_slow", bind=True)
def recovery_slow(self, marker: str, seconds: float) -> dict:
    """시작할 때마다 marker 파일에 '프로세스 ID' 한 줄을 남기고 seconds 동안 일한다 (몇 번 배달됐는지 세기용)."""
    with open(marker, "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
    time.sleep(seconds)
    return {"done": True, "pid": os.getpid()}
