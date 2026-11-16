"""
PostgreSQL + PostGIS 연결 · 세션 관리 + 데이터 저장소(Store).

[연결] get_engine() / SessionLocal() / db_status()
  - DATABASE_URL(.env · docker-compose)로 연결한다. 처음 쓸 때 만든다(지연 생성).
  - DB 가 없어도(로컬에서 도커 없이 실행 · 테스트) 서버는 메모리 저장소로 계속 동작한다.

[저장소] Store (지금은 메모리 · Phase ④에서 테이블 저장으로 교체)
  - 데이터(사용자 · 화물 · 기사 · 이상 감지 · 설정)를 가지고,
    기능은 services/ 의 Mixin(인증 · 주문 · 매칭 · 정산)에서 가져온다.
  - 서버를 재시작하면 core/seed.py 데이터로 초기화된다.
"""

from __future__ import annotations

import hashlib
import os
import random
import threading
from datetime import datetime, timedelta

from ..config import DEFAULT_SETTINGS, settings
from ..engines.matcher import spatial as geo
from ..models.order import DONE, _iso
from ..services.auth_service import AuthMixin
from ..services.billing_service import BillingMixin
from ..services.matching_service import MatchingMixin
from ..services.order_service import OrderMixin
from ..services.review_service import ReviewMixin
from ..services.tracking_service import TrackingMixin
from . import seed

# ------------------------------------------------------------------ PostgreSQL 연결
_engine = None
_session_factory = None


def get_engine():
    """SQLAlchemy 엔진 (연결 풀). pool_pre_ping: 끊긴 연결을 자동으로 다시 맺는다."""
    global _engine, _session_factory
    if _engine is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        _engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10,
                                connect_args={"connect_timeout": 3})
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def SessionLocal():  # noqa: N802 (SQLAlchemy 관례 이름)
    """with SessionLocal() as s: ... 형태로 쓰는 DB 세션."""
    get_engine()
    return _session_factory()


def db_status() -> dict:
    """DB 연결 + PostGIS 확장 확인 (서버 시작 시 · /health 에서 사용)."""
    try:
        from sqlalchemy import text
        with get_engine().connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            conn.commit()
            pg = conn.execute(text("SHOW server_version")).scalar()
            gis = conn.execute(text("SELECT postgis_lib_version()")).scalar()
        return {"ok": True, "postgres": pg, "postgis": gis}
    except Exception as e:  # noqa: BLE001 — 연결 실패는 상태로만 알린다
        return {"ok": False, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"}


# ------------------------------------------------------------------ DB 저장 (메모리 ↔ PostgreSQL 동기화)
def _enc(v):
    """JSONB 로 저장할 수 있게 datetime 을 {"$dt": iso} 로 바꾼다 (중첩 dict · list 포함)."""
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, dict):
        return {str(k): _enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    return v


def _dec(v):
    if isinstance(v, dict):
        if set(v) == {"$dt"}:
            return datetime.fromisoformat(v["$dt"])
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def _point(latlon: tuple[float, float]) -> str:
    """(lat, lon) → PostGIS geography WKT (경도 먼저)."""
    return f"SRID=4326;POINT({latlon[1]:.6f} {latlon[0]:.6f})"


class DbSync:
    """
    Store(메모리 작업 사본)를 PostgreSQL 에 저장 · 복원한다.

    - 저장: 요청이 끝날 때마다 행별 JSON 해시를 비교해 '바뀐 행만' UPSERT, 사라진 행은 DELETE.
      (조회 요청은 3초에 한 번으로 묶어 운행 진행률 등 시뮬레이션 변화만 반영)
    - 복원: 서버 시작 시 테이블에 데이터가 있으면 그대로 불러온다 → 재시작해도 데이터 유지.
    - 초기화: 관제 화면 [데모 데이터 초기화] → 테이블 비우고 시드 데이터로 다시 채움.
    """

    def __init__(self):
        self.hashes: dict[tuple[str, str], str] = {}
        self.last_save = 0.0
        self.error: str | None = None

    def create_tables(self) -> bool:
        from ..models import Base
        from ..models import audit as _audit, metrics as _metrics, order as _order, review as _review, user as _user
        assert _order.OrderRow and _user.UserRow and _review.ReviewRow and _audit.AuditRow  # 테이블 등록
        assert _metrics.MatchingMetricRow
        try:
            from sqlalchemy import text
            with get_engine().begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            Base.metadata.create_all(get_engine())
            with get_engine().begin() as conn:  # 예전 버전 DB 에 해시 체인 컬럼 추가 (있으면 그대로)
                conn.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS previous_hash VARCHAR(64)"))
                conn.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS current_hash VARCHAR(64)"))
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
            return False

    # ---- 메모리 → 행
    @staticmethod
    def _rows(store: "Store") -> dict[str, list[dict]]:
        from ..models.audit import AuditRow
        from ..models.order import AnomalyRow, AppStateRow, OrderRow
        from ..models.metrics import MatchingMetricRow
        from ..models.review import ReviewRow
        from ..models.user import AuthTokenRow, DriverRow, ShipperRow, UserRow
        orders = []
        for o in store.orders.values():
            data = {k: v for k, v in o.items() if k != "packing_by_truck"}  # 차종별 재계산 캐시는 저장 안 함
            orders.append({"id": o["id"], "shipper_id": o["shipper_id"], "driver_id": o["driver_id"],
                           "status": o["status"], "title": o["title"], "truck_type": o["truck_type"],
                           "origin": o["origin"], "destination": o["destination"],
                           "origin_geom": _point(geo.geocode(o["origin"])),
                           "dest_geom": _point(geo.geocode(o["destination"])),
                           "price": o["price"], "created_at": o["created_at"], "data": _enc(data)})
        return {
            UserRow: [{"email": u["email"], "id": u["id"], "role": u["role"], "name": u["name"],
                       "org_id": u["org_id"], "created_at": u["created_at"], "data": _enc(u)}
                      for u in store.users.values()],
            ShipperRow: [{"id": s["id"], "name": s["name"], "data": _enc(s)} for s in store.shippers.values()],
            DriverRow: [{"id": d["id"], "name": d["name"], "truck_type": d["truck_type"], "location": d["location"],
                         "geom": _point(geo.geocode(d["location"])), "month_runs": d["month_runs"],
                         "data": _enc(d)} for d in store.drivers.values()],
            AuthTokenRow: [{"token": t, "email": e} for t, e in store.tokens.items()],
            OrderRow: orders,
            AnomalyRow: [{"id": a["id"], "order_id": a["order_id"], "driver_id": a["driver_id"], "type": a["type"],
                          "status": a["status"], "at": a["at"], "data": _enc(a)} for a in store.anomalies.values()],
            AuditRow: [{"id": e["id"], "at": e["at"], "order_id": e["order_id"], "event": e["event"],
                        "actor": e["actor"][:60], "previous_hash": e.get("previous_hash"),
                        "current_hash": e.get("current_hash"), "data": _enc(e)} for e in store.audit],
            ReviewRow: [{"id": r["id"], "order_id": r["order_id"], "from_role": r["from_role"],
                         "to_role": r["to_role"], "to_id": r["to_id"], "stars": r["stars"], "at": r["at"],
                         "data": _enc(r)} for r in store.reviews.values()],
            MatchingMetricRow: [{"id": e["id"], "at": e["at"], "orders": e["orders"], "assigned": e["assigned"],
                                 "latency_ms": e["latency_ms"], "deadhead_reduction_pct": e["deadhead_reduction_pct"],
                                 "data": _enc(e)} for e in store.metrics_log],
            AppStateRow: [{"key": "settings", "value": _enc(store.settings)},
                          {"key": "settings_log", "value": {"items": _enc(store.settings_log)}},
                          {"key": "seq", "value": dict(store._seq)}],
        }

    def save(self, store: "Store", throttle: float = 0.0):
        import json
        import time

        from sqlalchemy import delete
        from sqlalchemy.dialects.postgresql import insert
        now = time.monotonic()
        if throttle and now - self.last_save < throttle:
            return
        self.last_save = now
        try:
            with SessionLocal() as s:
                for model, rows in self._rows(store).items():
                    pk = model.__mapper__.primary_key[0].name
                    table = model.__tablename__
                    changed, alive = [], set()
                    for r in rows:
                        key = r[pk]
                        alive.add(key)
                        h = hashlib.md5(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False)
                                        .encode()).hexdigest()
                        if self.hashes.get((table, key)) != h:
                            changed.append(r)
                            self.hashes[(table, key)] = h
                    if changed:
                        stmt = insert(model).values(changed)
                        cols = [c for c in changed[0] if c != pk]
                        s.execute(stmt.on_conflict_do_update(index_elements=[pk],
                                                             set_={c: stmt.excluded[c] for c in cols}))
                    gone = [k for (t, k) in self.hashes if t == table and k not in alive]
                    if gone:
                        s.execute(delete(model).where(getattr(model, pk).in_(gone)))
                        for k in gone:
                            self.hashes.pop((table, k), None)
                s.commit()
            self.error = None
        except Exception as e:  # noqa: BLE001 — 저장 실패해도 요청은 계속 처리 (다음 저장 때 다시 시도)
            self.hashes.clear()
            self.error = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"

    def load(self, store: "Store") -> bool:
        """테이블에 화물이 있으면 메모리로 불러온다. 없으면 False (→ 시드 데이터로 시작)."""
        from sqlalchemy import select

        from ..models.audit import AuditRow
        from ..models.order import AnomalyRow, AppStateRow, OrderRow
        from ..models.metrics import MatchingMetricRow
        from ..models.review import ReviewRow
        from ..models.user import AuthTokenRow, DriverRow, ShipperRow, UserRow
        with SessionLocal() as s:
            if s.execute(select(OrderRow.id).limit(1)).first() is None:
                return False
            store.users = {r.email: _dec(r.data) for r in s.scalars(select(UserRow))}
            store.shippers = {r.id: _dec(r.data) for r in s.scalars(select(ShipperRow))}
            store.drivers = {r.id: _dec(r.data) for r in s.scalars(select(DriverRow))}
            store.tokens = {r.token: r.email for r in s.scalars(select(AuthTokenRow))}
            store.orders = {r.id: _dec(r.data) for r in s.scalars(select(OrderRow).order_by(OrderRow.created_at))}
            store.anomalies = {r.id: _dec(r.data) for r in s.scalars(select(AnomalyRow))}
            store.audit = [_dec(r.data) for r in s.scalars(select(AuditRow).order_by(AuditRow.id))]
            store.reviews = {r.id: _dec(r.data) for r in s.scalars(select(ReviewRow))}
            store.metrics_log = [_dec(r.data) for r in s.scalars(select(MatchingMetricRow).order_by(MatchingMetricRow.id))]
            state = {r.key: r.value for r in s.scalars(select(AppStateRow))}
        store.settings = {**DEFAULT_SETTINGS, **_dec(state.get("settings", {}))}
        store.settings_log = _dec(state.get("settings_log", {}).get("items", []))
        store._seq = dict(state.get("seq", {}))
        self.hashes.clear()  # 다음 저장 때 전체를 한 번 비교해 맞춘다
        return True

    def wipe(self):
        from sqlalchemy import delete

        from ..models.audit import AuditRow
        from ..models.order import AnomalyRow, AppStateRow, OrderRow
        from ..models.metrics import MatchingMetricRow
        from ..models.review import ReviewRow
        from ..models.user import AuthTokenRow, DriverRow, ShipperRow, UserRow
        with SessionLocal() as s:
            for model in (AuditRow, ReviewRow, MatchingMetricRow, AnomalyRow, OrderRow, AuthTokenRow, DriverRow, ShipperRow, UserRow,
                          AppStateRow):
                s.execute(delete(model))
            s.commit()
        self.hashes.clear()


class Store(AuthMixin, OrderMixin, MatchingMixin, BillingMixin, ReviewMixin, TrackingMixin):
    def __init__(self, persistent: bool = False):
        """persistent=True: PostgreSQL 에 저장 · 복원 (서버용). False: 메모리만 (테스트용)."""
        self.lock = threading.RLock()
        self.db: DbSync | None = None
        self.db_info = "메모리 저장 (DB 미사용)"
        if persistent:
            db = DbSync()
            if db.create_tables():
                self.db = db
                if db.load(self):
                    self._after_load()
                    self.db_info = f"PostgreSQL 에서 화물 {len(self.orders)}건 불러옴"
                else:
                    self.reset()
                    db.save(self)
                    self.db_info = f"PostgreSQL 에 시드 데이터 저장 (화물 {len(self.orders)}건)"
                return
            self.db_info = f"메모리 저장 (DB 연결 실패: {db.error})"
        self.reset()

    def _after_load(self):
        """DB 에서 불러온 뒤, 저장하지 않는 실행 상태(시뮬레이션 시계 · 배경 차량)를 다시 만든다.
        예전 버전 데이터면 새 필드(번호판 용도 · 톤수 · 사업자 확인 · 해시 체인)를 채운다."""
        from ..engines import compliance_engine as comp
        from ..engines.fsm.order_fsm import rebuild_chain
        now = datetime.now()
        self.started_at = self.last_tick = now
        self.rng = random.Random(7)
        self.bg_fleet = self._make_background_fleet()
        self.reviews = getattr(self, "reviews", None) or {}
        self.metrics_log = getattr(self, "metrics_log", None) or []
        self._metric_seq = max((int(e["id"].split("-")[1]) for e in self.metrics_log), default=0)
        self.tracking = {}
        for d in self.drivers.values():
            d.setdefault("plate_type", comp.classify_plate(d["plate"])["plate_type"])
            d.setdefault("tonnage", comp.tonnage(d["truck_type"]))
        for s in self.shippers.values():
            s.setdefault("biz_check", comp.mock_business_status(s.get("biz_no")))
        if any(not e.get("current_hash") for e in self.audit):
            rebuild_chain(self.audit)  # 해시 체인 도입 전 감사 로그 → 한 번 다시 엮는다
        self._audit_seq = max((int(e["id"].split("-")[1]) for e in self.audit), default=0)

    def persist(self, throttle: float = 0.0):
        """바뀐 데이터를 DB 에 저장 (DB 를 안 쓰면 아무것도 안 함)."""
        if self.db:
            self.db.save(self, throttle)

    def reset_all(self) -> dict:
        """관제 [데모 데이터 초기화]: 시드 데이터로 되돌린다. 데모 계정의 로그인은 유지."""
        tokens = dict(self.tokens)
        if self.db:
            self.db.wipe()
        self.reset()
        self.tokens = {t: e for t, e in tokens.items() if e in self.users}
        self.persist()
        return {"ok": True, "orders": len(self.orders), "drivers": len(self.drivers),
                "storage": "PostgreSQL" if self.db else "메모리"}

    # ------------------------------------------------------------------ setup
    def reset(self):
        now = datetime.now()
        self.started_at = now
        self.last_tick = now
        self.rng = random.Random(7)
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        from ..engines import compliance_engine as comp
        self.shippers = {s["id"]: {**s, "biz_check": comp.mock_business_status(s["biz_no"])} for s in seed.SHIPPERS}
        self.drivers = {d["id"]: {**d, "plate_type": comp.classify_plate(d["plate"])["plate_type"],
                                  "tonnage": comp.tonnage(d["truck_type"])} for d in seed.DRIVERS}
        self.orders: dict[str, dict] = {}
        self.anomalies: dict[str, dict] = {}
        self.audit: list[dict] = []   # FSM 상태 전이 · 재무 변경 감사 로그 (SHA-256 해시 체인)
        self._audit_seq = 0
        self.reviews: dict[str, dict] = {}   # 화주-기사 상호 리뷰
        self.tracking: dict[str, dict] = {}  # 운행별 GPS 판정 상태 (이탈 연속 횟수 · 계획 ETA)
        self.metrics_log: list[dict] = []    # 매칭 라운드 성능 로그 (matching_metrics_logs)
        self._metric_seq = 0
        self._seq: dict[str, int] = {}
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings_log: list[dict] = []

        for u in seed.USERS:
            self._create_user(**u, password=seed.DEMO_PASSWORD)

        order_ids = [self._insert_order(o) for o in seed.orders(now)]
        self._seed_packing()
        self._seed_match_history()
        self._seed_audit()
        reviews = []
        for r in seed.reviews():
            idx = r.pop("order_index", None)
            reviews.append({**r, "order_id": order_ids[idx] if idx is not None else r["order_id"]})
        self._seed_reviews(reviews, now)
        for i, a in enumerate(seed.anomalies(now), start=1):
            idx = a.pop("order_index")
            order = self.orders[order_ids[idx]] if idx is not None else None
            aid = f"LOG-{a['at']:%Y%m%d}-{90 - i * 7:03d}"
            driver_id = order["driver_id"] if order else a.pop("driver_id")
            self.anomalies[aid] = {
                "id": aid, "order_id": order["id"] if order else None, "driver_id": driver_id,
                "resolved_at": a["at"] + timedelta(minutes=2, seconds=15) if a["status"] == "RESOLVED" else None,
                **a,
            }
        self.bg_fleet = self._make_background_fleet()
        self._matching_tick()  # 매칭 대기 화물에 바로 자동 매칭

    def _seed_packing(self):
        """시드 화물에 적재 계획을 붙인다 (자동 매칭 · 차주 적재 가이드 확인용)."""
        from ..engines.packer.engine import Options
        from ..engines.packer.models import ItemSpec
        from ..engines.packer.samples import SAMPLES
        from ..engines.packer.service import plan
        for o in self.orders.values():
            name = o.pop("packing_sample", None)
            items = o.pop("packing_items", None)
            if name:
                items = SAMPLES[name]["items"]
            if not items:
                continue
            res = plan([ItemSpec(**i) for i in items], truck_type=o["truck_type"], cold_chain=o["cold_chain"],
                       options=Options(record_steps=False))
            if res["unplaced"] or any(c["status"] == "fail" for c in res["constraints"]):
                continue  # 시드 품목이 그 차종에 안 맞으면 계획 없이 둔다
            o["packing"] = {k: res[k] for k in ("truck", "placements", "metrics", "constraints", "options")} \
                | {"items": [dict(i) for i in items]}

    def _seed_match_history(self):
        """이미 배차된 시드 화물의 매칭 기록 · 시드 거절 이력."""
        for o in self.orders.values():
            hist = o.pop("seed_match", None)
            if o["driver_id"]:
                hist = hist or {}
                pk = o["packing"]
                vol = pk["metrics"]["volume_util"] if pk else hist.get("volume_util")
                wgt = pk["metrics"]["weight_util"] if pk else None
                util = max(vol or 0, wgt or 0) if vol is not None else None
                km = hist.get("pickup_km", 10.0)
                o["match"] = {"driver_id": o["driver_id"], "pickup_km": km, "volume_util": vol, "weight_util": wgt,
                              "util": util, "score": self._score(km, util) if util is not None else None,
                              "offers": 1, "by": "auto"}
                offered = o["created_at"] + (o["assigned_at"] - o["created_at"]) / 3
                self._log(o, "offered", o["driver_id"], offered)
                self._log(o, "accepted", o["driver_id"], o["assigned_at"])
            for driver_id in o["declined"]:
                self._log(o, "offered", driver_id, o["created_at"] + timedelta(seconds=20))
                self._log(o, "declined", driver_id, o["created_at"] + timedelta(seconds=55))

    def _seed_audit(self):
        """시드 화물의 지난 상태 변경을 감사 로그로 복원 (등록 → 배차 → 운행 → 하차 → 청구 → 결제)."""
        from ..engines.fsm.order_fsm import audit_entry
        from ..services.billing_service import MockPG
        events = []
        for o in self.orders.values():
            shipper = self._actor("shipper", o["shipper_id"])
            driver = self._actor("driver", o["driver_id"]) if o["driver_id"] else None
            total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
            events.append((o["created_at"], o, "order", "create", None, "MATCHING", shipper, o["price"]))
            if o["assigned_at"]:
                events.append((o["assigned_at"], o, "order", "assign", "MATCHING", "ASSIGNED", driver, None))
            if o["status"] in ("IN_TRANSIT", "DELIVERED", "SETTLED"):
                start = o["started_at"] or (o["assigned_at"] or o["created_at"]) + timedelta(minutes=40)
                events.append((start, o, "order", "start", "ASSIGNED", "IN_TRANSIT", driver, None))
            if o["delivered_at"]:
                events.append((o["delivered_at"], o, "order", "deliver", "IN_TRANSIT", "DELIVERED", driver, None))
                events.append((o["delivered_at"], o, "billing", "deliver", None, "UNBILLED", driver, None))
            if o["billing"] in ("REQUESTED", "PAID"):
                at = o["delivered_at"] + timedelta(hours=3)
                events.append((at, o, "billing", "request", "UNBILLED", "REQUESTED", driver, total))
            if o["billing"] == "PAID":
                at = o["delivered_at"] + timedelta(days=o["paid_days"] or 2.4)
                o["payment"] = MockPG.approve(o["id"], total, "corp_card", at=at)  # 시드 결제도 모의 PG 기록
                o["payments"] = [o["payment"]]
                events.append((at, o, "billing", "pay", "REQUESTED", "PAID", shipper, total))
                events.append((at, o, "order", "settle", "DELIVERED", "SETTLED", shipper, total))
        for at, o, kind, ev, before, after, actor, amount in sorted(events, key=lambda e: e[0]):
            audit_entry(self, o, kind, ev, before, after, actor, note="시드 데이터", amount=amount, at=at)

    def _next_id(self, prefix: str, when: datetime) -> str:
        key = f"{prefix}-{when:%Y%m%d}"
        self._seq[key] = self._seq.get(key, 0) + 1
        return f"{key}-{self._seq[key]:02d}"

    def _insert_order(self, data: dict) -> str:
        data = dict(data)
        seed_match = data.pop("match", None)
        o = {
            "cold_chain": False, "urgent": False, "incentive": 0, "driver_id": None,
            "assigned_at": None, "started_at": None, "delivered_at": None,
            "progress": 0.0, "speed": 0, "temp_set": None, "temp_now": None,
            "deviated": False, "delay_minutes": 0, "match_note": None,
            "billing": None, "extra_fee": 0, "toll_fee": 0, "memo": "", "requested_at": None, "paid_days": None,
            "pod": None, "packing": None,
            # 자동 매칭
            "offer": None,          # 진행 중인 제안 {driver_id, offered_at, expires_at, pickup_km, util, score, ...}
            "declined": [],         # 거절/응답 없음 기사 (재공고·재매칭 시 초기화)
            "match_log": [],        # 제안/수락/거절 이력
            "match_fail": None,     # 후보가 없을 때 원인 목록
            "match_fail_at": None,
            "match": None,          # 배차 확정 시 매칭 결과 기록
            **data,
        }
        o["declined"] = list(o["declined"])
        o["posted_at"] = o.get("posted_at") or o["created_at"]
        o["seed_match"] = seed_match
        o["id"] = self._next_id("ORD", o["created_at"])
        o["distance_km"] = geo.road_km(o["origin"], o["destination"])
        if o["cold_chain"] and o["temp_set"] is None:
            o["temp_set"] = -18.0
        if o["cold_chain"] and o["temp_now"] is None and o["status"] == "IN_TRANSIT":
            o["temp_now"] = o["temp_set"]
        if o["status"] in DONE and o["pod"] is None:
            o["pod"] = self._make_pod(o, receiver="박지성", at=o["delivered_at"])
            o["progress"] = 100.0
        if o["status"] == "SETTLED" and o["paid_days"] is None:
            o["paid_days"] = 2.4
        self.orders[o["id"]] = o
        return o["id"]

    @staticmethod
    def _make_pod(o: dict, receiver: str, at: datetime) -> dict:
        digest = hashlib.sha256(f"{o['id']}|{receiver}|{at.isoformat()}".encode()).hexdigest()
        return {"receiver": receiver, "signed_at": _iso(at), "hash": "0x" + digest[:12].upper()}

    def _make_background_fleet(self) -> list[dict]:
        rng = random.Random(42)
        regions = ["서울", "경기", "인천", "충남", "충북", "경북", "경남", "부산", "대구", "전남"]
        letters = "가나다라마바사아자차카타파하"
        fleet = []
        for i in range(30):
            a, b = rng.choice(geo.CORRIDORS)
            kind = rng.choices(["transit", "cold", "empty"], weights=[55, 20, 25])[0]
            truck = rng.choice(["11톤 냉동탑차", "5톤 냉동탑차"]) if kind == "cold" else \
                rng.choice(["1톤 카고", "5톤 윙바디", "11톤 탑차", "11톤 탑차", "25톤 무진동"])
            fleet.append({
                "id": f"BG-{i:03d}",
                "plate": f"{rng.choice(regions)} {rng.randint(70, 99)}{rng.choice(letters)} {rng.randint(1000, 9999)}",
                "a": geo.HUBS[a], "b": geo.HUBS[b], "seg_km": max(geo.route_km(geo.HUBS[a], geo.HUBS[b]), 1.0),
                "path": geo.route(geo.HUBS[a], geo.HUBS[b]),  # 도로를 따라 거점 사이를 오간다
                "phase": rng.random() * 2, "speed": rng.randint(68, 92), "kind": kind, "truck_type": truck,
                "jitter": (rng.uniform(-0.05, 0.05), rng.uniform(-0.05, 0.05)),
            })
        return fleet


# 서버용 저장소. 테스트는 BAECHA_DB=0 으로 DB 를 쓰지 않는다 (tests/_check.py).
store = Store(persistent=os.getenv("BAECHA_DB", "1") != "0")
