"""
비동기 3D 빈패킹 연산 워커 (Celery 작업)

화주 등록 화면의 적재 시뮬레이션과 같은 엔진(engines/packer)을 워커에서 돌린다.
품목이 많아 오래 걸리는 계산을 API 서버 밖으로 빼서, 서버가 다른 요청을 계속 처리할 수 있게 한다.

API:  POST /packing/plan-async → {task_id}   ·   GET /packing/tasks/{task_id} → 상태 · 결과
"""

from __future__ import annotations

import time

from src.workers.celery_app import celery_app


@celery_app.task(name="packing.plan", bind=True)
def plan_packing(self, payload: dict) -> dict:
    """payload = PackPlanIn 과 같은 형식 (items · truck_type · cold_chain · options)."""
    from src.engines.packer import service
    from src.schemas.order_schema import PackPlanIn

    body = PackPlanIn(**payload)
    t0 = time.perf_counter()
    try:
        res = service.plan(body.item_specs(), truck_type=body.truck_type, cold_chain=body.cold_chain,
                           options=body.options.to_engine(record_steps=False))
    except service.PackingError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "worker": self.request.hostname, "seconds": round(time.perf_counter() - t0, 3),
            "result": res}
