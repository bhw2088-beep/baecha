"""관제 · 추적 API: 기사 운행(상차/하차 + e-POD) · 전국 차량 위치 · 관제 현황 · 이상 감지."""

import asyncio

import hashlib
import os

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ...config import settings
from ...core.database import store
from ...core.errors import ApiError
from ...core.redis_client import available as redis_available, get_redis
from ...core.security import current_user, require
from ...schemas.matching_schema import AskIn, LocationBatchIn, LocationIn, PingBatchIn, PingIn
from ...workers import gps_stream_worker

router = APIRouter(tags=["tracking"])
Driver = Depends(require("driver"))
Admin = Depends(require("admin"))


# ------------------------------------------------------------------ 차주 운행
@router.get("/driver/profile")
async def driver_profile(user: dict = Driver):
    return store.driver_profile(user["org_id"])


@router.post("/driver/runs/{order_id}/start")
async def start_run(order_id: str, user: dict = Driver):
    return store.start_run(user["org_id"], order_id)


POD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "pod_photos")
POD_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@router.post("/driver/runs/{order_id}/complete")
async def complete_run(order_id: str, receiver: str = Form(...), photo: UploadFile = File(...),
                       user: dict = Driver):
    """하차 완료 · 전자인수증 (multipart). 인수증 사진 필수 → SHA-256 을 인수증 해시에 묶고 파일을 보관한다."""
    data = await photo.read()
    if not data:
        raise ApiError(400, "인수증 사진을 첨부해 주세요.")
    if len(data) > 5 * 1024 * 1024:
        raise ApiError(400, "인수증 사진은 5MB 이하만 가능합니다.")
    ext = POD_TYPES.get(photo.content_type or "")
    if not ext:
        raise ApiError(400, "인수증 사진은 JPG · PNG · WEBP 이미지만 가능합니다.")
    meta = {"filename": photo.filename, "size": len(data), "content_type": photo.content_type,
            "sha256": hashlib.sha256(data).hexdigest()}
    res = store.complete_run(user["org_id"], order_id, receiver, photo=meta)
    os.makedirs(POD_DIR, exist_ok=True)
    with open(os.path.join(POD_DIR, f"{order_id}{ext}"), "wb") as f:  # 화주 · 관제가 볼 수 있게 보관
        f.write(data)
    return res


@router.get("/orders/{order_id}/pod/photo")
async def pod_photo(order_id: str, user: dict = Depends(current_user)):
    """인수증 사진 (화주 · 배정 기사 · 관제만). 시드 · 이전 데이터는 사진 파일이 없다."""
    store.order_detail(order_id, user)  # 접근 권한 확인
    for ext in POD_TYPES.values():
        path = os.path.join(POD_DIR, f"{order_id}{ext}")
        if os.path.exists(path):
            return FileResponse(path)
    raise ApiError(404, "보관된 인수증 사진이 없습니다 (시드 · 이전 데이터).")


# ------------------------------------------------------------------ 관제
@router.get("/admin/summary", dependencies=[Admin])
async def admin_summary():
    return store.admin_summary()


@router.get("/admin/fleet", dependencies=[Admin])
async def fleet():
    return store.fleet()


@router.get("/admin/anomalies", dependencies=[Admin])
async def anomalies(type: str = "ALL", status: str = "ALL"):
    return store.list_anomalies(type, status)


@router.post("/admin/anomalies/{anomaly_id}/resolve", dependencies=[Admin])
async def resolve(anomaly_id: str):
    return store.resolve_anomaly(anomaly_id)


@router.post("/admin/orders/{order_id}/delay-ack", dependencies=[Admin])
async def delay_ack(order_id: str):
    """AI 관제 브리핑 '도착 지연' 조치: 화주에게 새 도착 시각을 안내했음을 기록 (그 지연 단계까지 브리핑에서 숨김)."""
    return store.ack_delay(order_id)


@router.post("/admin/reset", dependencies=[Admin])
async def reset_demo():
    """[데모 데이터 초기화] DB 를 비우고 시드 데이터로 다시 채운다 (시연 전 · 테스트 후). 데모 계정 로그인은 유지."""
    return store.reset_all()


@router.get("/admin/audit", dependencies=[Admin])
async def audit(order_id: str | None = None, financial: bool = False, limit: int = 200):
    """감사 로그 (FSM 상태 전이 · 청구 · 결제 · 환불). financial=true 면 재무 변경만."""
    return store.audit_log(order_id, financial, min(limit, 1000))


# ------------------------------------------------------------------ 시뮬레이터 전용 (내부)
def _sim_key(x_sim_key: str = Header(default="")):
    if x_sim_key != settings.sim_api_key:
        raise HTTPException(status_code=401, detail="시뮬레이터 키가 올바르지 않습니다.")


@router.get("/internal/runs", dependencies=[Depends(_sim_key)], include_in_schema=False)
async def sim_runs():
    """가상 기사 시뮬레이터가 2초마다 읽는 운행 목록 (도로 경로 · 진행률 · 정체 여부)."""
    return store.sim_runs()


# ------------------------------------------------------------------ 실시간 GPS 핑 (Phase 3)
async def _enqueue(msgs: list[dict]) -> bool:
    """Redis Streams 로 보낸다 (XADD). Redis 가 없으면(로컬 메모리 실행) 바로 처리한다."""
    if redis_available():
        def xadd():
            pipe = get_redis().pipeline()
            for m in msgs:
                pipe.xadd(gps_stream_worker.STREAM, {k: str(v) for k, v in m.items() if v is not None},
                          maxlen=gps_stream_worker.MAXLEN, approximate=True)
            pipe.execute()
        try:
            await asyncio.to_thread(xadd)
            return True
        except Exception:  # noqa: BLE001 — Redis 일시 장애 → 바로 처리
            pass
    for m in msgs:
        store.ingest_ping(m)
    return False


def _ping_msg(body: PingIn, driver_id: str, source: str) -> dict:
    return {"order_id": body.order_id, "driver_id": driver_id, "lat": f"{body.lat:.6f}", "lon": f"{body.lon:.6f}",
            "speed": int(body.speed), "ts": body.ts, "source": source}


@router.post("/tracking/ping", status_code=202)
async def ping(body: PingIn, authorization: str = Header(default=""), x_sim_key: str = Header(default="")):
    """GPS 핑 인입: 배정 기사 대조(불법 재위탁 방지) → Redis Streams XADD → 워커가 이탈 · 지연 판정.
    기사 앱: Authorization: Bearer <차주 토큰>  /  가상 기사: X-Sim-Key + driver_id."""
    if x_sim_key:
        if x_sim_key != settings.sim_api_key:
            raise HTTPException(status_code=401, detail="시뮬레이터 키가 올바르지 않습니다.")
        driver_id, source = body.driver_id, "virtual_driver"
    else:
        user = store.user_by_token(authorization.removeprefix("Bearer ").strip())
        if not user or user["role"] != "driver":
            raise HTTPException(status_code=401, detail="차주 로그인이 필요합니다.")
        driver_id, source = user["org_id"], "driver_app"
    store.check_ping(body.order_id, driver_id, source)
    queued = await _enqueue([_ping_msg(body, driver_id, source)])
    return {"accepted": True, "queued": queued, "pipeline": "Redis Streams" if queued else "직접 처리 (Redis 없음)"}


@router.post("/tracking/ping/batch", dependencies=[Depends(_sim_key)],
             include_in_schema=False)
async def ping_batch(body: PingBatchIn):
    """가상 기사 시뮬레이터 전용: 여러 운행의 핑을 한 번에. 배정 기사가 아닌 핑은 거절 목록으로."""
    ok, rejected = [], []
    for p in body.pings:
        try:
            store.check_ping(p.order_id, p.driver_id, "virtual_driver")
            ok.append(_ping_msg(p, p.driver_id, "virtual_driver"))
        except ApiError as e:
            rejected.append({"order_id": p.order_id, "status": e.status, "detail": e.detail})
    queued = await _enqueue(ok) if ok else False
    return {"accepted": len(ok), "rejected": rejected, "queued": queued}


# ------------------------------------------------------------------ 공차 기사 실시간 위치 (텔레메트리)
@router.post("/tracking/location", status_code=202)
async def report_location(body: LocationIn, user: dict = Depends(current_user)):
    """기사 앱: 공차(배차 대기) 중 현재 위치 보고 → 매칭 거리 · PostGIS 반경 필터 · 관제 지도에 반영."""
    if user["role"] != "driver":
        raise HTTPException(status_code=403, detail="차주만 위치를 보고할 수 있습니다.")
    return {"result": store.report_location(user["org_id"], body.lat, body.lon, body.speed)}


@router.post("/tracking/location/batch", dependencies=[Depends(_sim_key)], status_code=202, include_in_schema=False)
async def report_location_batch(body: LocationBatchIn):
    """텔레메트리 시뮬레이터 전용: 공차 기사 여러 명의 위치를 한 번에 (X-Sim-Key)."""
    counts: dict[str, int] = {}
    for loc in body.locations:
        r = store.report_location(loc.driver_id or "", loc.lat, loc.lon, loc.speed)
        counts[r] = counts.get(r, 0) + 1
    return counts


@router.get("/admin/drivers/{driver_id}/detail", dependencies=[Admin])
async def driver_detail(driver_id: str):
    """관제 우측 상세 패널: 기사 연락처 · 현재 운행(계획 경로 · 이탈 · 지연) · 감사 로그 · 인수증 · 리뷰."""
    return store.admin_driver_detail(driver_id)


@router.get("/admin/tracking", dependencies=[Admin])
async def tracking_status():
    """운행별 경로 이탈 거리 · 연속 이탈 횟수(디바운싱) · 지연 · 재위탁 의심 핑."""
    return store.tracking_status()


@router.get("/admin/audit/verify", dependencies=[Admin])
async def audit_verify():
    """감사 로그 SHA-256 해시 체인 위변조 검사."""
    from ...engines.fsm.order_fsm import verify_chain
    return verify_chain(store.audit)


@router.get("/admin/gps", dependencies=[Admin])
async def gps_status():
    """GPS 수신 파이프라인 상태 (Redis Streams 소비 건수 등)."""
    return {**gps_stream_worker.stats, "live": store.admin_summary()["gps_live"]}


@router.get("/admin/perf", dependencies=[Admin])
async def perf_status(since: float | None = None):
    """서버 성능 기록 (core/perf.py): DB 저장 단계별 시간 · 이벤트 루프 멈춤 · 1초 넘는 요청 · 긴 GC.
    since = epoch 초 (이 시각 이후만). 부하 시뮬레이터가 단계별로 나눠 본다."""
    from ...core import perf
    return perf.snapshot(since)


@router.get("/admin/agent/briefing", dependencies=[Admin])
async def agent_briefing():
    """규칙 기반 AI 관제 보조: 매칭 실패 · 운행 이상 · 지연 · 정산 · 가입 심사 · 평판 · 수급을 점검해 조치와 안내 문구 초안을 제안."""
    from ...engines.ai import gemini_agent, rule_agent
    return {**rule_agent.briefing(store), "llm": gemini_agent.available(settings), "llm_model": settings.gemini_model}


@router.post("/admin/agent/ask", dependencies=[Admin])
async def agent_ask(body: AskIn):
    """자연어 관제 명령 (Gemini Function Calling · 키가 없으면 규칙 기반). 설정 변경은 서버가 검증 후 적용."""
    from ...engines.ai import gemini_agent, rule_agent
    if not gemini_agent.available(settings):
        return {**rule_agent.handle_command(store, body.text), "fallback": "GEMINI_API_KEY 없음 → 규칙 기반"}
    res = await asyncio.to_thread(gemini_agent.ask, store, settings, body.text)  # 네트워크 대기는 스레드에서
    res["applied"] = gemini_agent.apply_pending(store, res.pop("pending", []))
    return res


@router.get("/admin/agent/incident/{anomaly_id}", dependencies=[Admin])
async def agent_incident(anomaly_id: str):
    """이상 감지 한 건 → 액션 아이템 형태 인시던트 리포트 (Gemini · 없으면 규칙)."""
    from ...engines.ai import gemini_agent
    return await asyncio.to_thread(gemini_agent.incident_report, store, settings, anomaly_id)


@router.get("/admin/agent/tuning", dependencies=[Admin])
async def agent_tuning():
    """거절률 · 평점 · 수급 기반 LightGBM 임계값 · 가중치 조절 가이드 (자동 적용 안 함)."""
    from ...engines.ai import rule_agent
    return rule_agent.tuning_guide(store)
