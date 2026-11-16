"""
인증 서비스: 회원가입(화주 · 차주 · 관제) · 로그인 · 토큰.

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from ..config import ADMIN_APPROVAL_CODE
from ..core.errors import ApiError
from ..core.security import _hash_pw
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

    def register(self, *, role, email, password, name, phone, company=None, biz_no=None,
                 plate=None, truck_type=None, base_location=None, approval_code=None, documents=None) -> dict:
        email = email.lower().strip()
        if email in self.users:
            raise ApiError(409, "이미 가입된 이메일입니다.")
        if len(password) < 8:
            raise ApiError(400, "비밀번호는 8자 이상이어야 합니다.")
        if role == "shipper":
            if not company or not biz_no:
                raise ApiError(400, "기업명과 사업자등록번호를 입력해 주세요.")
            org_id = f"S-{len(self.shippers) + 1:03d}"
            self.shippers[org_id] = {"id": org_id, "name": company, "biz_no": biz_no}
            sub = company
        elif role == "driver":
            if not plate or not truck_type or not biz_no:
                raise ApiError(400, "차량 번호, 차종, 사업자등록번호를 입력해 주세요.")
            if truck_type not in pricing.TRUCK_TYPES:
                raise ApiError(400, "지원하지 않는 차종입니다.")
            if base_location and not geo.is_known(base_location):
                raise ApiError(400, "주 대기 지역을 지역 목록에서 선택해 주세요.")
            org_id = f"D-{len(self.drivers) + 1:03d}"
            self.drivers[org_id] = {"id": org_id, "name": name, "phone": phone, "plate": plate,
                                    "truck_type": truck_type, "location": base_location or "대전 중구", "month_runs": 0,
                                    # 관제 '컴플라이언스'에서 서류 확인 후 승인해야 배차를 받을 수 있다
                                    "approved": False,
                                    "approval": {"status": "PENDING", "requested_at": _iso(datetime.now()),
                                                 "biz_no": biz_no, "documents": documents or [],
                                                 "decided_at": None, "decided_by": None, "reason": None}}
            sub = f"{truck_type} · {plate}"
        elif role == "admin":
            if approval_code != ADMIN_APPROVAL_CODE:
                raise ApiError(403, "관제센터 승인 코드가 올바르지 않습니다.")
            org_id, sub = None, "중앙 관제 센터"
        else:
            raise ApiError(400, "회원 유형이 올바르지 않습니다.")
        self._create_user(email=email, role=role, name=name, password=password, phone=phone,
                          org_id=org_id, sub=sub, documents=documents or [])
        return self.login(email, password)

    # ------------------------------------------------------------------ 관제 컴플라이언스: 차주 가입 승인
    def compliance_drivers(self) -> list[dict]:
        """가입 심사 목록 (승인 대기 먼저). 시드 기사는 승인 완료 상태."""
        users = {u["org_id"]: u for u in self.users.values() if u["role"] == "driver"}
        rows = []
        for d in self.drivers.values():
            a = d.get("approval") or {"status": "APPROVED", "requested_at": None, "documents": [], "reason": None,
                                      "decided_at": None, "decided_by": "시드 데이터", "biz_no": None}
            u = users.get(d["id"], {})
            rows.append({"id": d["id"], "name": d["name"], "phone": d["phone"], "plate": d["plate"],
                         "truck_type": d["truck_type"], "location": d["location"], "email": u.get("email"),
                         **a, "approved": d.get("approved", True)})
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
        if approve:
            self._matching_tick()  # 승인되면 바로 매칭 후보가 된다
        return next(r for r in self.compliance_drivers() if r["id"] == driver_id)
