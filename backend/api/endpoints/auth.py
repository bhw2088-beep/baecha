"""인증 · 공용 API: 회원가입/로그인/로그아웃, 서버 상태, 화면 메타데이터."""

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile

from ...core import seed
from ...core.database import db_status, store
from ...core.errors import ApiError
from ...core.redis_client import redis_status
from ...core.security import current_user, require, tick_store
from ...engines.matcher import spatial as geo
from ...schemas.auth_schema import DecisionIn, LoginIn
from ...services import billing_service as pricing

router = APIRouter(tags=["auth"])


@router.get("/health")
def health():
    """서버 · DB(PostgreSQL+PostGIS) · Redis 상태. DB/Redis 확인은 블로킹 호출이라 동기 함수(스레드풀)로 둔다."""
    return {"ok": True, "db": db_status(), "redis": redis_status()}


@router.get("/meta", dependencies=[Depends(tick_store)])
async def meta():
    return {
        "truck_types": list(pricing.TRUCK_TYPES),
        "cold_types": sorted(pricing.COLD_TYPES),
        "regions": geo.region_options(),  # 상차지 · 하차지 · 기사 대기 지역 드롭다운
        "matching": store.settings_view(),  # 자동 매칭 설정 (관제가 바꾸면 화면 안내 문구도 같이 바뀜)
        "demo_accounts": [
            {"role": u["role"], "email": u["email"], "name": u["name"], "sub": u["sub"]}
            for u in store.users.values() if u["email"] in {d["email"] for d in seed.USERS}
        ][:3],
        "stats": store.public_stats(),
        "hubs": [{"name": k, "lat": v[0], "lon": v[1], "weight": geo.HUB_WEIGHTS.get(k)} for k, v in geo.HUBS.items()],
    }


@router.post("/auth/login")
async def login(body: LoginIn):
    return store.login(body.email, body.password)


@router.post("/auth/register")
async def register(
    role: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    phone: str = Form(""),
    company: str | None = Form(None),
    biz_no: str | None = Form(None),
    plate: str | None = Form(None),
    truck_type: str | None = Form(None),
    base_location: str | None = Form(None),
    approval_code: str | None = Form(None),
    owner_name: str | None = Form(None),      # 대표자명 (화주 필수 · 진위확인 / 차주 선택)
    open_date: str | None = Form(None),       # 개업일자 YYYYMMDD (화주 필수 · 진위확인 / 차주 선택)
    license_photo: UploadFile | None = File(None),
    cargo_license_photo: UploadFile | None = File(None),   # 화물운송종사 자격증
    vehicle_photo: UploadFile | None = File(None),
):
    documents = []
    for kind, f in (("license", license_photo), ("cargo_license", cargo_license_photo), ("vehicle", vehicle_photo)):
        if f is not None and f.filename:
            data = await f.read()
            if len(data) > 5 * 1024 * 1024:
                raise ApiError(400, "첨부 파일은 5MB 이하만 가능합니다.")
            # TODO: 오브젝트 스토리지(S3 등)에 저장. 지금은 파일명/크기만 기록한다.
            documents.append({"kind": kind, "filename": f.filename, "size": len(data)})
    return store.register(role=role, email=email, password=password, name=name, phone=phone,
                          company=company, biz_no=biz_no, plate=plate, truck_type=truck_type,
                          base_location=base_location, approval_code=approval_code, documents=documents,
                          owner_name=owner_name, open_date=open_date)


@router.get("/compliance/check")
async def compliance_check(biz_no: str = "", plate: str = ""):
    """가입 전 미리 확인 (로그인 불필요): 사업자번호 Mod-10 · 사업 상태, 번호판 용도."""
    from ...config import settings
    from ...engines import compliance_engine as comp
    out = {}
    if biz_no:
        out["business"] = comp.verify_business(biz_no, api_key=settings.nts_api_key)
    if plate:
        out["plate"] = comp.classify_plate(plate)
    return out


@router.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return store.public_user(user)


@router.post("/auth/logout")
async def logout(authorization: str = Header(default=""), _: dict = Depends(current_user)):
    store.logout(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


# ------------------------------------------------------------------ 관제 컴플라이언스: 차주 가입 승인
Admin = Depends(require("admin"))


@router.get("/admin/compliance/drivers", dependencies=[Admin])
async def compliance_drivers():
    """차주 가입 심사 목록 (승인 대기 먼저 · 제출 서류 파일명/크기)."""
    return store.compliance_drivers()


@router.post("/admin/compliance/drivers/{driver_id}")
async def decide_driver(driver_id: str, body: DecisionIn, user: dict = Admin):
    """승인(approve=true) → 바로 매칭 후보 · 반려(approve=false, reason 필수)."""
    return store.decide_driver(driver_id, body.approve, by=user["name"], reason=body.reason)
