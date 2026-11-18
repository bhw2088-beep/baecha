"""
인증 서비스: 회원가입(화주 · 차주 · 관제) · 로그인 · 토큰.

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from ..config import ADMIN_APPROVAL_CODE, settings
from ..core.errors import ApiError
from ..core.security import _hash_pw
from ..engines import compliance_engine as comp
from ..engines.matcher import spatial as geo
from ..models.order import _iso
from . import billing_service as pricing


class AuthMixin:
    def _create_user(self, email, role, name, password, phone="", org_id=None, sub="", **extra):
        salt = secrets.token_hex(8)
        self.users[email.lower()] = {
            "id": f"U-{len(self.users) + 1:04d}", "email": email.lower(), "role": role, "name": name,
            "phone": phone, "org_id": org_id, "sub": sub, "salt": salt, "pw": _hash_pw(password, salt),
            "created_at": datetime.now(), **extra,
        }
        return self.users[email.lower()]

    @staticmethod
    def public_user(u: dict) -> dict:
        return {k: u[k] for k in ("id", "email", "role", "name", "phone", "org_id", "sub")}

    def login(self, email: str, password: str) -> dict:
        u = self.users.get(email.lower().strip())
        if not u or _hash_pw(password, u["salt"]) != u["pw"]:
            raise ApiError(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
        token = secrets.token_urlsafe(24)
        self.tokens[token] = u["email"]
        return {"token": token, "user": self.public_user(u)}

    def logout(self, token: str):
        self.tokens.pop(token, None)

    def user_by_token(self, token: str) -> dict | None:
        email = self.tokens.get(token)
        return self.users.get(email) if email else None

    def _check_business(self, biz_no: str | None, owner: str | None, start_date: str | None) -> dict:
        """사업자 확인 (Phase 0): Mod-10 → 국세청 API(키 있을 때) 또는 모의 상태. 계속사업자가 아니면 가입 거절."""
        if not biz_no:
            raise ApiError(400, "사업자등록번호를 입력해 주세요.")
        res = comp.verify_business(biz_no, api_key=settings.nts_api_key, owner=owner, start_date=start_date)
        if not res["ok"]:
            raise ApiError(400, f"사업자등록번호 확인 실패: {res['label']}"
                                + (f" ({res['note']})" if res.get("note") else "") + f" · {res['source_label']}")
        return res

    def register(self, *, role, email, password, name, phone, company=None, biz_no=None,
                 plate=None, truck_type=None, base_location=None, approval_code=None, documents=None,
                 owner_name=None, open_date=None) -> dict:
        email = email.lower().strip()
        if email in self.users:
            raise ApiError(409, "이미 가입된 이메일입니다.")
        if len(password) < 8:
            raise ApiError(400, "비밀번호는 8자 이상이어야 합니다.")
        # 가입 자동 검증(컴플라이언스 규칙): 통과 · 차단 모두 감사 로그에 남겨 KPI '검증 자동화율'로 집계한다
        try:
            if role in ("shipper", "driver"):
                self._check_duplicates(role, phone, biz_no, plate)
            if role == "shipper":
                # 기업명 · 사업자번호 · 대표자명 · 개업일자 4개 모두 필수, 하나의 사업자 등록 정보와 모두 일치해야 가입
                if not all((v or "").strip() for v in (company, biz_no, owner_name, open_date)):
                    raise ApiError(400, "기업명 · 사업자등록번호 · 대표자명 · 개업일자를 모두 입력해 주세요.")
                biz = comp.verify_business_identity(biz_no, company, owner_name, open_date,
                                                    api_key=settings.nts_api_key)
                if not biz["ok"]:
                    raise ApiError(400, f"사업자 진위확인 실패: {biz['label']}"
                                        + (f" ({biz['note']})" if biz.get("note") else "") + f" · {biz['source_label']}")
                org_id = f"S-{len(self.shippers) + 1:03d}"
                self.shippers[org_id] = {"id": org_id, "name": company.strip(), "biz_no": biz["biz_no"], "biz_check": biz,
                                         "owner_name": owner_name.strip(), "open_date": open_date}
                sub = company
            elif role == "driver":
                if not plate or not truck_type or not biz_no:
                    raise ApiError(400, "차량 번호, 차종, 사업자등록번호를 입력해 주세요.")
                if truck_type not in pricing.TRUCK_TYPES:
                    raise ApiError(400, "지원하지 않는 차종입니다.")
                if base_location and not geo.is_known(base_location):
                    raise ApiError(400, "주 대기 지역을 지역 목록에서 선택해 주세요.")
                p = comp.classify_plate(plate)
                if p["plate_type"] == "INVALID":
                    raise ApiError(400, "차량 번호 형식이 올바르지 않습니다. 예: 경기 88바 7712")
                if not p["dispatchable"]:
                    raise ApiError(400, f"{p['label']} 차량은 등록할 수 없습니다. "
                                        "화물 운송은 사업용 노란색 번호판(바 · 사 · 아 · 자)만 가능합니다.")
                missing = comp.missing_driver_docs(documents or [])
                if missing:
                    raise ApiError(400, "필수 서류를 첨부해 주세요: " + ", ".join(missing))
                biz = self._check_business(biz_no, owner_name or name, open_date)
                org_id = f"D-{len(self.drivers) + 1:03d}"
                self.drivers[org_id] = {"id": org_id, "name": name, "phone": phone, "plate": plate.strip(),
                                        "plate_type": p["plate_type"], "tonnage": comp.tonnage(truck_type),
                                        "truck_type": truck_type, "location": base_location or "대전 중구", "month_runs": 0,
                                        # 관제 '컴플라이언스'에서 서류 확인 후 승인해야 배차를 받을 수 있다
                                        "approved": False,
                                        "approval": {"status": "PENDING", "requested_at": _iso(datetime.now()),
                                                     "biz_no": biz["biz_no"], "biz_check": biz,
                                                     "documents": documents or [],
                                                     "decided_at": None, "decided_by": None, "reason": None}}
                sub = f"{truck_type} · {plate.strip()}"
            elif role == "admin":
                if approval_code != ADMIN_APPROVAL_CODE:
                    raise ApiError(403, "관제센터 승인 코드가 올바르지 않습니다.")
                org_id, sub = None, "중앙 관제 센터"
            else:
                raise ApiError(400, "회원 유형이 올바르지 않습니다.")
        except ApiError as e:
            if role in ("shipper", "driver"):
                self._signup_audit(role, False, e.detail)
            raise
        if role in ("shipper", "driver"):
            self._signup_audit(role, True, sub)
        self._create_user(email=email, role=role, name=name, password=password, phone=phone,
                          org_id=org_id, sub=sub, documents=documents or [])
        return self.login(email, password)

    @staticmethod
    def _digits(value: str | None) -> str:
        return "".join(c for c in (value or "") if c.isdigit())

    @staticmethod
    def _plate_key(value: str | None) -> str:
        return "".join((value or "").split())  # '경기 88바 7712' = '경기88바7712'

    def _check_duplicates(self, role: str, phone: str | None, biz_no: str | None, plate: str | None):
        """가입 중복 방지 (이메일 중복은 위에서 따로 확인).
        - 연락처: 화주 · 차주 · 관제 전체에서 한 번만 (숫자만 비교: 010-1234-5678 = 01012345678)
        - 사업자번호: 차주는 1인 1번호(다른 차주 · 화주와 겹치면 안 됨),
                      화주는 같은 회사 담당자 여러 명 허용 · 단 차주로 등록된 번호는 사용 불가
        - 차량 번호: 차주 전체에서 한 대 한 명 (띄어쓰기 무시)"""
        p = self._digits(phone)
        if p:
            used = {self._digits(u.get("phone")) for u in self.users.values()}
            used |= {self._digits(d.get("phone")) for d in self.drivers.values()}
            if p in used:
                raise ApiError(409, "이미 가입된 연락처입니다.")
        b = self._digits(biz_no)
        if b:
            driver_biz = {self._digits(d.get("biz_no") or (d.get("approval") or {}).get("biz_no"))
                          for d in self.drivers.values()} - {""}
            shipper_biz = {self._digits(s.get("biz_no")) for s in self.shippers.values()} - {""}
            if role == "driver" and b in driver_biz | shipper_biz:
                raise ApiError(409, "이미 등록된 사업자등록번호입니다.")
            if role == "shipper" and b in driver_biz:
                raise ApiError(409, "차주로 등록된 사업자등록번호라 화주 가입에 사용할 수 없습니다.")
        if role == "driver" and plate:
            if self._plate_key(plate) in {self._plate_key(d.get("plate")) for d in self.drivers.values()}:
                raise ApiError(409, "이미 등록된 차량 번호입니다.")

    def _signup_audit(self, role: str, passed: bool, detail: str):
        from ..engines.fsm.order_fsm import audit_entry
        who = "화주" if role == "shipper" else "차주"
        audit_entry(self, None, "compliance", "signup_pass" if passed else "signup_reject", None,
                    "AUTO_PASS" if passed else "AUTO_REJECT", "규칙 엔진", note=f"{who} · {detail}", order_id="-")

    @staticmethod
    def driver_dispatchable(d: dict) -> bool:
        """매칭 후보 자격: 관제 승인 + 사업용 노란색 번호판 + 블랙리스트 아님."""
        return (d.get("approved", True) and d.get("plate_type", "COMMERCIAL_YELLOW") == "COMMERCIAL_YELLOW"
                and not d.get("blacklisted"))

    # ------------------------------------------------------------------ 관제 컴플라이언스: 차주 가입 승인
    def compliance_drivers(self) -> list[dict]:
        """가입 심사 목록 (승인 대기 먼저). 시드 기사는 승인 완료 상태. 번호판 · 사업자 · 서류 · 평점 함께."""
        users = {u["org_id"]: u for u in self.users.values() if u["role"] == "driver"}
        rows = []
        for d in self.drivers.values():
            a = d.get("approval") or {"status": "APPROVED", "requested_at": None, "documents": [], "reason": None,
                                      "decided_at": None, "decided_by": "시드 데이터", "biz_no": d.get("biz_no"),
                                      "biz_check": d.get("biz_check")}
            u = users.get(d["id"], {})
            cp = comp.driver_compliance(d["plate"], d["truck_type"], a.get("documents") or [], a.get("biz_check"))
            if d.get("approval") is None:  # 시드 기사는 서류 심사를 마친 것으로 본다
                cp["missing_docs"] = []
                cp["issues"] = [i for i in cp["issues"] if not i.startswith("서류")]
                cp["ok"] = not cp["issues"]
            rows.append({"id": d["id"], "name": d["name"], "phone": d["phone"], "plate": d["plate"],
                         "truck_type": d["truck_type"], "location": d["location"], "email": u.get("email"),
                         **a, "approved": d.get("approved", True), "compliance": cp,
                         "dispatchable": self.driver_dispatchable(d),
                         "rating": self.driver_rating(d["id"]) if hasattr(self, "driver_rating") else None,
                         "blacklisted": bool(d.get("blacklisted")), "blacklist": d.get("blacklist")})
        order = {"PENDING": 0, "REJECTED": 1, "APPROVED": 2}
        return sorted(rows, key=lambda r: (order.get(r["status"], 3), r["requested_at"] or ""), reverse=False)

    def decide_driver(self, driver_id: str, approve: bool, by: str, reason: str = "") -> dict:
        d = self.drivers.get(driver_id)
        if not d:
            raise ApiError(404, "기사를 찾을 수 없습니다.")
        a = d.get("approval")
        if not a or a["status"] != "PENDING":
            raise ApiError(409, "승인 대기 중인 가입 신청이 아닙니다.")
        if not approve and not reason.strip():
            raise ApiError(400, "반려 사유를 입력해 주세요.")
        a.update(status="APPROVED" if approve else "REJECTED", decided_at=_iso(datetime.now()), decided_by=by,
                 reason=None if approve else reason.strip())
        d["approved"] = approve
        from ..engines.fsm.order_fsm import audit_entry
        audit_entry(self, None, "compliance", "approve" if approve else "reject", "PENDING", a["status"], by,
                    note=f"{driver_id} {d['name']} · {d['plate']}" + (f" · 사유: {reason.strip()}" if reason else ""),
                    order_id=driver_id)
        if approve:
            self._matching_tick()  # 승인되면 바로 매칭 후보가 된다
        return next(r for r in self.compliance_drivers() if r["id"] == driver_id)
