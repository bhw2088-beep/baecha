"""주문 API: 화주 화물 등록(3-Step → 자동 매칭) · 화물 조회 · 전자인수증 · 3D 적재 시뮬레이션."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from ...core.database import store
from ...core.errors import ApiError
from ...core.security import current_user, require
from ...engines.packer import service as packing_service
from ...engines.packer.samples import SAMPLES
from ...engines.packer.trucks import TRUCKS
from ...schemas.order_schema import OrderIn, PackPlanIn
from ...services import billing_service as pricing

router = APIRouter(tags=["orders"])
Shipper = Depends(require("shipper"))


# ------------------------------------------------------------------ 화주
@router.get("/shipper/summary")
async def shipper_summary(user: dict = Shipper):
    return store.shipper_summary(user["org_id"])


@router.get("/shipper/orders")
async def shipper_orders(user: dict = Shipper):
    return store.shipper_orders(user["org_id"])


@router.post("/shipper/orders", status_code=201)
async def create_order(body: OrderIn, user: dict = Shipper):
    if body.truck_type not in pricing.TRUCK_TYPES:
        raise ApiError(400, "지원하지 않는 차종입니다.")
    packing = None
    if body.items:
        # 클라이언트 결과를 믿지 않고 서버에서 같은 엔진으로 재계산 (스레드풀 — 이벤트 루프를 막지 않음)
        try:
            packing = await run_in_threadpool(
                packing_service.plan, body.item_specs(), truck_type=body.truck_type, cold_chain=body.cold_chain,
                options=body.packing_options.to_engine(record_steps=False))
        except packing_service.PackingError as e:
            raise ApiError(400, str(e))
        failed = [c["name"] for c in packing["constraints"] if c["status"] == "fail"]
        if packing["unplaced"] or failed:
            raise ApiError(400, f"{body.truck_type}에 전량 적재할 수 없습니다 "
                                f"(미적재 {len(packing['unplaced'])}개{', 위반: ' + ', '.join(failed) if failed else ''}).")
    data = body.model_dump(exclude={"items", "packing_options"})
    return store.create_order(user["org_id"], data, packing=packing,
                              items=[i.model_dump() for i in body.items or []])


# ------------------------------------------------------------------ 화물 공통 (권한은 store 에서 확인)
@router.get("/orders/{order_id}")
async def order_detail(order_id: str, user: dict = Depends(current_user)):
    return store.order_detail(order_id, user)


@router.get("/orders/{order_id}/pod")
async def order_pod(order_id: str, user: dict = Depends(current_user)):
    order = store.order_detail(order_id, user)
    if not order["pod"]:
        raise ApiError(404, "아직 전자인수증이 발급되지 않은 화물입니다.")
    return order


@router.get("/orders/{order_id}/packing")
async def order_packing(order_id: str, user: dict = Depends(current_user)):
    """등록 시 저장된 적재 계획 (팀 공통 포맷 + 지표). 차주 적재 가이드 뷰어가 사용할 엔드포인트."""
    return store.order_packing(order_id, user)


# ------------------------------------------------------------------ 3D 빈패킹
@router.get("/packing/meta", dependencies=[Depends(current_user)])
async def packing_meta():
    return {
        "trucks": [t.as_dict() for t in TRUCKS.values()],
        "samples": SAMPLES,
        "max_boxes": packing_service.MAX_BOXES,
        "defaults": {"merit": "wall", "sort": "area_height", "min_support": 0.8, "lifo": True},
    }


@router.post("/packing/plan", dependencies=[Depends(current_user)])
def packing_plan(body: PackPlanIn):
    """CPU 연산이므로 동기 함수로 두어 스레드풀에서 실행 (다른 요청을 막지 않음). 저장소는 건드리지 않는다."""
    try:
        return packing_service.plan(body.item_specs(), truck_type=body.truck_type, cold_chain=body.cold_chain,
                                    options=body.options.to_engine())
    except packing_service.PackingError as e:
        raise ApiError(400, str(e))


# ------------------------------------------------------------------ 3D 빈패킹 비동기 (Celery 워커)
@router.post("/packing/plan-async", dependencies=[Depends(current_user)], status_code=202)
def packing_plan_async(body: PackPlanIn):
    """적재 시뮬레이션을 Celery 워커에 맡기고 작업 번호를 바로 돌려준다 (무거운 계산용)."""
    from ...workers.packing_tasks import plan_packing
    try:
        task = plan_packing.apply_async(args=[body.model_dump()], expires=300)
    except Exception as e:  # noqa: BLE001 — 브로커(Redis) 연결 실패
        raise ApiError(503, f"작업 큐에 연결할 수 없습니다: {str(e)[:120]}")
    return {"task_id": task.id, "status": "PENDING"}


@router.get("/packing/tasks/{task_id}", dependencies=[Depends(current_user)])
def packing_task(task_id: str):
    """Celery 작업 상태: PENDING(대기) → STARTED(계산 중) → SUCCESS(완료, result 포함) / FAILURE."""
    from celery.result import AsyncResult

    from ...workers.celery_app import celery_app
    r = AsyncResult(task_id, app=celery_app)
    out = {"task_id": task_id, "status": r.status}
    if r.successful():
        out.update(r.result)
    elif r.failed():
        out["error"] = str(r.result)[:200]
    return out
