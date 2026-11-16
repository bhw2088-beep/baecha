"""
주문 서비스: 화물 등록 · 조회 · 운행(상차/하차 + e-POD) · 실시간 위치 시뮬레이션 · 이상 감지 · 관제 현황.

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from ..config import TIME_SCALE
from ..core.errors import ApiError
from ..engines.fsm import order_fsm
from ..engines.matcher import spatial as geo
from ..models.order import (ACTIVE_RUN, ANOMALY_TYPE_LABEL, BILLING_LABEL, DONE, MATCH_BY_LABEL,
                            STATUS_LABEL, _iso)
from . import billing_service as pricing


class OrderMixin:
    # ------------------------------------------------------------------ 상태 전이 (FSM) · 감사 로그
    def _actor(self, role: str, org_id: str | None) -> str:
        """감사 로그의 '누가' (예: 차주 D-001 김철수)."""
        if role == "driver":
            d = self.drivers.get(org_id, {})
            return f"차주 {org_id} {d.get('name', '')}".strip()
        if role == "shipper":
            s = self.shippers.get(org_id, {})
            return f"화주 {org_id} {s.get('name', '')}".strip()
        return "관제"

    def _fsm(self, o: dict, event: str, actor: str, **kw):
        """engines/fsm/order_fsm.py 로만 상태를 바꾼다. 허용되지 않는 전이는 409."""
        try:
            order_fsm.transition(self, o, event, actor, **kw)
        except order_fsm.TransitionError as e:
            raise ApiError(409, str(e))

    @staticmethod
    def _audit_view(e: dict) -> dict:
        return {**e, "at": _iso(e["at"])}

    def audit_log(self, order_id: str | None = None, financial: bool = False, limit: int = 200) -> list[dict]:
        rows = [e for e in self.audit if (order_id is None or e["order_id"] == order_id)
                and (not financial or e["financial"])]
        return [self._audit_view(e) for e in sorted(rows, key=lambda e: e["id"], reverse=True)[:limit]]

    # ------------------------------------------------------------------ simulation
    def tick(self):
        """요청이 들어올 때마다 지난 시간만큼 운행 상태를 진행시키고, 자동 매칭을 한 번 돌린다."""
        now = datetime.now()
        dt = (now - self.last_tick).total_seconds()
        if dt > 0:
            self.last_tick = now
            sim_hours = dt * TIME_SCALE / 3600
            for o in self.orders.values():
                if o["status"] != "IN_TRANSIT":
                    continue
                if self._open_anomaly(o["id"], "STAGNATION"):
                    o["speed"] = 0
                elif self._gps_live(o):
                    pass  # 가상 기사 시뮬레이터가 Redis Streams 로 보낸 GPS 가 위치 · 속도를 갱신 중
                elif o["speed"] > 0:
                    o["speed"] = max(60, min(98, o["speed"] + self.rng.randint(-3, 3)))
                    o["progress"] = min(97.0, o["progress"] + sim_hours * o["speed"] / max(o["distance_km"], 1) * 100)
                if o["cold_chain"] and o["temp_set"] is not None and self._open_anomaly(o["id"], "COLD") is None:
                    o["temp_now"] = round(o["temp_set"] + self.rng.uniform(-0.4, 0.4), 1)
        self._matching_tick()

    # ---- GPS 수신 (workers/gps_stream_worker.py 가 Redis Streams 에서 읽어 호출)
    GPS_FRESH_SEC = 15

    def _gps_live(self, o: dict) -> bool:
        at = o.get("gps_at")
        return at is not None and (datetime.now() - at).total_seconds() < self.GPS_FRESH_SEC

    # GPS 한 건 반영(apply_gps · ingest_ping)은 services/tracking_service.py (경로 이탈 · 지연 · 재위탁 판정)

    def sim_runs(self) -> list[dict]:
        """가상 기사 시뮬레이터에게 줄 운행 목록 (도로 경로 · 현재 진행률 · 정체 여부)."""
        out = []
        for o in self.orders.values():
            if o["status"] != "IN_TRANSIT":
                continue
            d = self.drivers.get(o["driver_id"], {})
            out.append({"order_id": o["id"], "driver_id": o["driver_id"], "plate": d.get("plate"),
                        "distance_km": o["distance_km"], "progress": round(o["progress"], 2), "speed": o["speed"],
                        "stopped": self._open_anomaly(o["id"], "STAGNATION") is not None,
                        "route": self._lonlat(self._path(o))})
        return out

    def _open_anomaly(self, order_id: str, kind: str | None = None) -> dict | None:
        for a in self.anomalies.values():
            if a["order_id"] == order_id and a["status"] == "UNSOLVED" and (kind is None or a["type"] == kind):
                return a
        return None

    @staticmethod
    def _path(o: dict) -> list[tuple[float, float]]:
        """상차지 → 하차지 도로 경로 [(lat, lon), ...] (경로 캐시가 없으면 직선)."""
        return geo.route(geo.geocode(o["origin"]), geo.geocode(o["destination"]))

    def _position(self, o: dict) -> tuple[float, float]:
        path = self._path(o)
        if o["status"] in ("MATCHING", "ASSIGNED"):
            return path[0]
        if o["status"] in DONE:
            return path[-1]
        g = o.get("gps")
        if g and self._gps_live(o):
            return g["lat"], g["lon"]  # 실시간 GPS (지터 · 이탈 포함 실제 보고 위치)
        p, i = geo.along(path, o["progress"] / 100)  # 도로를 따라 진행률만큼 이동
        return geo.offset_perpendicular(path[i], path[i + 1], p, 0.09) if o["deviated"] else p

    @staticmethod
    def _lonlat(path: list[tuple[float, float]]) -> list[list[float]]:
        return [[round(lon, 5), round(lat, 5)] for lat, lon in path]

    def _eta(self, o: dict) -> tuple[int | None, str | None]:
        if o["status"] not in ACTIVE_RUN:
            return None, None
        remaining = o["distance_km"] * (1 - o["progress"] / 100)
        speed = o["speed"] if o["speed"] > 0 else 75
        minutes = int(remaining / speed * 60 + o["delay_minutes"])
        if o["status"] == "ASSIGNED":
            minutes += 40  # 상차 작업 예상 시간
        eta = datetime.now() + timedelta(minutes=minutes)
        day = "오늘" if eta.date() == datetime.now().date() else "내일"
        return minutes, f"{day} {eta:%H:%M}"

    # ------------------------------------------------------------------ views
    def driver_brief(self, driver_id: str | None) -> dict | None:
        d = self.drivers.get(driver_id) if driver_id else None
        if not d:
            return None
        r = self.driver_rating(driver_id)
        return {**{k: d[k] for k in ("id", "name", "phone", "plate", "truck_type")},
                "rating": r["avg"], "reviews": r["count"]}  # 기사 평점 (화주 리뷰)

    def order_view(self, o: dict) -> dict:
        minutes, clock = self._eta(o)
        anomaly = self._open_anomaly(o["id"])
        lat, lon = self._position(o)
        total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
        wait = (datetime.now() - o["posted_at"]).total_seconds() if o["status"] == "MATCHING" else None
        return {
            "id": o["id"], "title": o["title"], "truck_type": o["truck_type"],
            "cold_chain": o["cold_chain"], "urgent": o["urgent"],
            "origin": o["origin"], "destination": o["destination"], "distance_km": o["distance_km"],
            "price": o["price"], "incentive": o["incentive"], "fare": o["price"] + o["incentive"],
            "extra_fee": o["extra_fee"], "toll_fee": o["toll_fee"], "total_fee": total, "memo": o["memo"],
            "status": o["status"], "status_label": STATUS_LABEL[o["status"]],
            "anomaly": {"id": anomaly["id"], "type": anomaly["type"], "text": anomaly["text"]} if anomaly else None,
            "shipper": self.shippers.get(o["shipper_id"], {}).get("name", "-"),
            "shipper_rating": {k: v for k, v in self.shipper_rating(o["shipper_id"]).items() if k in ("avg", "count")},
            "driver": self.driver_brief(o["driver_id"]),
            "progress": round(o["progress"], 1), "speed": o["speed"],
            "eta_minutes": minutes, "eta_clock": clock, "delay_minutes": o["delay_minutes"],
            "temp_set": o["temp_set"], "temp_now": o["temp_now"],
            "created_at": _iso(o["created_at"]), "posted_at": _iso(o["posted_at"]),
            "assigned_at": _iso(o["assigned_at"]), "delivered_at": _iso(o["delivered_at"]),
            "period": o["delivered_at"].strftime("%Y-%m") if o["delivered_at"] else None,
            "wait_seconds": int(wait) if wait is not None else None,
            "billing": o["billing"], "billing_label": BILLING_LABEL.get(o["billing"], "-"),
            "payment": o.get("payment"),  # 모의 PG 결제 (거래번호 · 승인번호 · 상태)
            "pod": o["pod"], "lat": lat, "lon": lon, "deviated": o["deviated"],
            "gps": {k: (o.get("gps") or {}).get(k) for k in ("dist_km", "off_count", "source")}
            if o.get("gps") else None,
            "required_tonnage": o.get("required_tonnage"),
            "review": {k: (self.reviews.get(f"REV-{o['id']}-{r}") or {}).get("stars") for k, r in
                       (("by_shipper", "S"), ("by_driver", "D"))} if o["status"] in DONE else None,
            "packing": self._packing_summary(o),
            "matching": self._matching_view(o),
            "fare_est": self._fare_est(o),
            "match": {**o["match"], "by_label": MATCH_BY_LABEL[o["match"]["by"]]} if o["match"] else None,
            "match_log": o["match_log"],
            "route": self._lonlat(self._path(o)),  # 도로 경로 [[lon, lat], ...] (pydeck PathLayer)
        }


    def _packing_summary(self, o: dict, truck_type: str | None = None) -> dict | None:
        """배차된 화물은 '배정 차량' 기준 계획(재계산 캐시)을, 아니면 화주가 등록한 계획을 요약."""
        pk = o["packing"]
        if not pk:
            return None
        truck_type = truck_type or (self.drivers[o["driver_id"]]["truck_type"] if o["driver_id"] else None)
        if truck_type:
            pk = self._plan_for_truck(o, truck_type)
        return {"truck": pk["truck"]["name"], **{k: pk["metrics"][k] for k in
                                                ("placed", "volume_util", "weight_util", "loaded_weight")}}

    def _get_order(self, order_id: str) -> dict:
        o = self.orders.get(order_id)
        if not o:
            raise ApiError(404, "화물을 찾을 수 없습니다.")
        return o

    def _active_run_of(self, driver_id: str) -> dict | None:
        return next((o for o in self.orders.values()
                     if o["driver_id"] == driver_id and o["status"] in ACTIVE_RUN), None)

    def _driver_coord(self, d: dict) -> tuple[float, float]:
        run = self._active_run_of(d["id"])
        return self._position(run) if run else geo.geocode(d["location"])

    # ------------------------------------------------------------------ public
    def public_stats(self) -> dict:
        fleet = self.fleet()
        return {"vehicles": len(fleet), "moving": sum(1 for v in fleet if v["kind"] != "empty" and v["speed"] > 0),
                "orders_today": sum(1 for o in self.orders.values()
                                    if o["created_at"].date() == datetime.now().date())}

    # ------------------------------------------------------------------ shipper
    def shipper_orders(self, shipper_id: str) -> list[dict]:
        rows = [o for o in self.orders.values()
                if o["shipper_id"] == shipper_id and o["status"] not in DONE]
        rows.sort(key=lambda o: o["created_at"], reverse=True)
        return [self.order_view(o) for o in rows]

    def create_order(self, shipper_id: str, data: dict, packing: dict | None = None,
                     items: list[dict] | None = None) -> dict:
        for field, label in (("origin", "상차지"), ("destination", "하차지")):
            if not geo.is_known(data[field]):
                raise ApiError(400, f"{label} '{data[field]}' 의 위치를 찾을 수 없습니다. 지역 목록에서 선택해 주세요.")
        if geo.geocode(data["origin"]) == geo.geocode(data["destination"]):
            raise ApiError(400, "상차지와 하차지가 같은 지역입니다.")
        now = datetime.now()
        cold = data["truck_type"] in pricing.COLD_TYPES or data.get("cold_chain", False)
        oid = self._insert_order({
            "title": data["title"], "truck_type": data["truck_type"], "cold_chain": cold,
            "urgent": data.get("urgent", False), "origin": data["origin"], "destination": data["destination"],
            "price": data["price"], "shipper_id": shipper_id, "status": "MATCHING", "created_at": now,
            "temp_set": data.get("temp_set") if cold else None,
        })
        o = self.orders[oid]
        o.pop("seed_match", None)
        if packing:
            # 적재 계획 저장 (EP 스냅샷/후보 비교는 등록 화면 전용이라 제외)
            o["packing"] = {k: packing[k] for k in ("truck", "placements", "metrics", "constraints",
                                                    "options")} | {"items": items or []}
        order_fsm.audit_entry(self, o, "order", "create", None, "MATCHING", self._actor("shipper", shipper_id),
                              note=f"{o['origin']} → {o['destination']} · {o['truck_type']}", amount=o["price"], at=now)
        self._run_matching(o, now)  # 등록 즉시 자동 매칭
        return self.order_view(o)

    def order_packing(self, order_id: str, user: dict) -> dict:
        o = self._get_order(order_id)
        self._check_access(o, user)
        if not o["packing"]:
            raise ApiError(404, "적재 계획이 없는 화물입니다.")
        # 차주가 보면 '그 차주의 실제 차량' 기준, 배차된 화물이면 '배차된 차량' 기준으로 계획을 돌려준다
        truck = None
        if user["role"] == "driver":
            truck = self.drivers[user["org_id"]]["truck_type"]
        elif o["driver_id"]:
            truck = self.drivers[o["driver_id"]]["truck_type"]
        data = self._plan_for_truck(o, truck) if truck else o["packing"]
        from ..engines.packer.service import team_format
        return {**team_format(order_id, data), "metrics": data["metrics"], "constraints": data["constraints"],
                "placements": data["placements"], "unplaced": data.get("unplaced", []),
                "planned_truck": o["packing"]["truck"]["name"], "truck": data["truck"],
                "replanned": data is not o["packing"], "title": o["title"],
                "origin": o["origin"], "destination": o["destination"]}

    def _plan_for_truck(self, o: dict, truck_type: str) -> dict:
        """화주가 등록한 계획과 차량이 다르면 같은 품목·옵션으로 그 차량 기준 재계산 (결과는 화물에 캐시)."""
        pk = o["packing"]
        if truck_type == pk["truck"]["name"]:
            return pk
        cache = o.setdefault("packing_by_truck", {})
        if truck_type not in cache:
            from ..engines.packer.engine import Options
            from ..engines.packer.models import ItemSpec
            from ..engines.packer.service import plan
            opts = {k: v for k, v in pk["options"].items() if k in ("merit", "sort", "min_support", "lifo")}
            res = plan([ItemSpec(**i) for i in pk["items"]], truck_type=truck_type, cold_chain=o["cold_chain"],
                       options=Options(**opts, record_steps=False))
            cache[truck_type] = {k: res[k] for k in ("truck", "placements", "metrics", "constraints", "options",
                                                     "unplaced")} | {"items": pk["items"]}
        return cache[truck_type]

    def order_detail(self, order_id: str, user: dict) -> dict:
        o = self._get_order(order_id)
        self._check_access(o, user)
        return {**self.order_view(o), "audit": self.audit_log(order_id)[::-1]}  # 상태 변경 이력 (오래된 순)

    def _check_access(self, o: dict, user: dict):
        if user["role"] == "admin":
            return
        if user["role"] == "shipper" and o["shipper_id"] == user["org_id"]:
            return
        if user["role"] == "driver" and (o["driver_id"] == user["org_id"] or o["status"] == "MATCHING"):
            return  # 배정된 화물 + 매칭 대기 화물(배차 제안 · AI 추천 리스트에서 적재 계획 확인용)
        raise ApiError(403, "이 화물에 접근할 권한이 없습니다.")

    def shipper_history(self, shipper_id: str, q: str = "", status: str = "ALL", period: str = "ALL") -> dict:
        all_rows = [o for o in self.orders.values() if o["shipper_id"] == shipper_id and o["status"] in DONE]
        periods = sorted({o["delivered_at"].strftime("%Y-%m") for o in all_rows}, reverse=True)
        q = q.lower().strip()
        rows = []
        for o in sorted(all_rows, key=lambda o: o["delivered_at"], reverse=True):
            v = self.order_view(o)
            driver_name = v["driver"]["name"] if v["driver"] else ""
            if q and q not in f"{v['id']} {v['title']} {driver_name}".lower():
                continue
            if status != "ALL" and v["status"] != status:
                continue
            if period != "ALL" and v["period"] != period:
                continue
            rows.append(v)
        return {"items": rows, "periods": periods}

    def shipper_summary(self, shipper_id: str) -> dict:
        now = datetime.now()
        mine = [o for o in self.orders.values() if o["shipper_id"] == shipper_id]
        recent = [o for o in mine if o["created_at"] >= now - timedelta(days=30)]
        matched = [(o["assigned_at"] - o["created_at"]).total_seconds() for o in mine if o["assigned_at"]]
        utils = [o["match"]["volume_util"] for o in mine if o["match"] and o["match"]["volume_util"] is not None]
        done = [o for o in mine if o["status"] in DONE]
        settled_30 = [o for o in done if o["status"] == "SETTLED" and o["delivered_at"] >= now - timedelta(days=30)]
        fee = lambda o: o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]  # noqa: E731
        return {
            "orders_30d": len(recent),
            "active": sum(1 for o in mine if o["status"] in ACTIVE_RUN),
            "matching": sum(1 for o in mine if o["status"] == "MATCHING"),
            "offered": sum(1 for o in mine if o["status"] == "MATCHING" and o["offer"]),
            "match_failed": sum(1 for o in mine if o["status"] == "MATCHING" and o["match_fail"]),
            "anomalies": sum(1 for o in mine if self._open_anomaly(o["id"])),
            "avg_match_seconds": int(mean(matched)) if matched else None,
            "avg_volume_util": round(mean(utils), 3) if utils else None,
            "settled_30d": sum(fee(o) for o in settled_30),
            "unpaid": sum(fee(o) for o in done if o["status"] == "DELIVERED"),
            "invoices_waiting": sum(1 for o in done if o["billing"] == "REQUESTED"),
            "pod_count": sum(1 for o in done if o["pod"]),
            "done_count": len(done),
        }

    def driver_profile(self, driver_id: str) -> dict:
        d = self.drivers[driver_id]
        run = self._active_run_of(driver_id)
        now = datetime.now()
        done = [o for o in self.orders.values() if o["driver_id"] == driver_id and o["status"] in DONE]
        fee = lambda o: o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]  # noqa: E731
        paid = [o for o in done if o["billing"] == "PAID"]
        paid_30 = [o for o in paid if o["delivered_at"] >= now - timedelta(days=30)]
        waiting = [o for o in done if o["billing"] == "REQUESTED"]
        offer = self._pending_offer_of(driver_id)
        state = ("운행중" if run and run["status"] == "IN_TRANSIT" else "상차지 이동" if run
                 else "배차 제안 도착" if offer else "공차 대기")
        return {
            **{k: d[k] for k in ("id", "name", "phone", "plate", "truck_type", "month_runs")},
            "location": d["location"], "state": state if d.get("approved", True) else "가입 승인 대기",
            "approved": d.get("approved", True), "approval": d.get("approval"),
            "plate_type": d.get("plate_type"), "tonnage": d.get("tonnage"), "rating": self.driver_rating(driver_id),
            "blacklisted": bool(d.get("blacklisted")), "blacklist": d.get("blacklist"),
            "dispatchable": self.driver_dispatchable(d),
            "run": self.order_view(run) if run else None,
            "offer_id": offer["id"] if offer else None,
            **self._driver_offer_stats(driver_id),
            "paid_30d": sum(fee(o) for o in paid_30), "paid_30d_count": len(paid_30),
            "receivable": sum(fee(o) for o in waiting), "receivable_count": len(waiting),
            "unbilled_count": sum(1 for o in done if o["billing"] == "UNBILLED"),
            "pod_rate": round(100 * sum(1 for o in done if o["pod"]) / len(done), 1) if done else 100.0,
            "avg_settle_days": round(mean(o["paid_days"] for o in paid if o["paid_days"]), 1) if paid else None,
        }

    def start_run(self, driver_id: str, order_id: str) -> dict:
        o = self._get_order(order_id)
        if o["driver_id"] != driver_id or o["status"] != "ASSIGNED":
            raise ApiError(400, "상차 완료 처리할 수 있는 화물이 아닙니다.")
        self._fsm(o, "start", self._actor("driver", driver_id))
        o["started_at"], o["speed"], o["progress"] = datetime.now(), 82, 0.0
        if o["cold_chain"]:
            o["temp_now"] = o["temp_set"]
        return self.order_view(o)

    def complete_run(self, driver_id: str, order_id: str, receiver: str) -> dict:
        o = self._get_order(order_id)
        if o["driver_id"] != driver_id or o["status"] != "IN_TRANSIT":
            raise ApiError(400, "배송 완료 처리할 수 있는 화물이 아닙니다.")
        if not receiver.strip():
            raise ApiError(400, "인수자 성함을 입력해 주세요.")
        now = datetime.now()
        pod = self._make_pod(o, receiver.strip(), now)
        self._fsm(o, "deliver", self._actor("driver", driver_id), note=f"인수자 {receiver.strip()} · 인수증 {pod['hash']}",
                  at=now)
        o.update(delivered_at=now, progress=100.0, speed=0, pod=pod)
        self.drivers[driver_id]["location"] = o["destination"]
        self.drivers[driver_id]["month_runs"] += 1
        for a in self.anomalies.values():
            if a["order_id"] == order_id and a["status"] == "UNSOLVED":
                a["status"], a["resolved_at"] = "RESOLVED", now
        return self.order_view(o)

    def driver_completed(self, driver_id: str) -> list[dict]:
        rows = [o for o in self.orders.values() if o["driver_id"] == driver_id and o["status"] in DONE]
        rows.sort(key=lambda o: o["delivered_at"], reverse=True)
        return [self.order_view(o) for o in rows]

    # ------------------------------------------------------------------ admin
    def fleet(self) -> list[dict]:
        out = []
        for d in self.drivers.values():
            run = self._active_run_of(d["id"])
            offer = None if run else self._pending_offer_of(d["id"])
            lat, lon = self._driver_coord(d)
            anomaly = self._open_anomaly(run["id"]) if run else None
            if anomaly:
                kind = "anomaly"
            elif not run:
                kind = "empty"
            else:
                kind = "cold" if run["cold_chain"] else "transit"
            status = (anomaly["text"] if anomaly else STATUS_LABEL[run["status"]] if run
                      else f"배차 제안 응답 대기 ({offer['id']})" if offer else "공차 대기")
            out.append({
                "id": d["id"], "plate": d["plate"], "driver": d["name"], "truck_type": d["truck_type"],
                "kind": kind, "lat": lat, "lon": lon, "speed": run["speed"] if run else 0,
                "status_text": status, "order_id": run["id"] if run else None,
                "cargo": run["title"] if run else (offer["title"] if offer else "-"),
                "temp": run["temp_now"] if run else None, "named": True,
                # 운행 중이면 상차지 → 하차지 도로 경로 (관제 지도 PathLayer)
                "path": self._lonlat(self._path(run)) if run else None,
            })
        elapsed_h = (datetime.now() - self.started_at).total_seconds() * TIME_SCALE / 3600
        for t in self.bg_fleet:
            if t["kind"] == "empty":
                lat, lon, speed = t["a"][0] + t["jitter"][0], t["a"][1] + t["jitter"][1], 0
            else:
                phase = (t["phase"] + elapsed_h * t["speed"] / t["seg_km"]) % 2
                (lat, lon), _ = geo.along(t["path"], phase if phase < 1 else 2 - phase)
                speed = t["speed"]
            out.append({"id": t["id"], "plate": t["plate"], "driver": "-", "truck_type": t["truck_type"],
                        "kind": t["kind"], "lat": lat, "lon": lon, "speed": speed,
                        "status_text": "공차 대기" if t["kind"] == "empty" else "운행중", "order_id": None,
                        "cargo": "-", "temp": None, "named": False, "path": None})
        return out

    def admin_summary(self) -> dict:
        fleet = self.fleet()
        moving = [v["speed"] for v in fleet if v["speed"] > 0]
        resolved = [(a["resolved_at"] - a["at"]).total_seconds() for a in self.anomalies.values() if a["resolved_at"]]
        unsolved = [a for a in self.anomalies.values() if a["status"] == "UNSOLVED"]
        matching = [o for o in self.orders.values() if o["status"] == "MATCHING"]
        return {
            "vehicles": len(fleet),
            "in_transit": sum(1 for v in fleet if v["kind"] in ("transit", "cold", "anomaly")),
            "empty": sum(1 for v in fleet if v["kind"] == "empty"),
            "cold": sum(1 for v in fleet if v["kind"] == "cold"),
            "avg_speed": round(mean(moving)) if moving else 0,
            "anomalies_open": len(unsolved),
            "avg_response_seconds": int(mean(resolved)) if resolved else None,
            "matching": len(matching),
            "offers_pending": sum(1 for o in matching if o["offer"]),
            "failed_matches": sum(1 for o in matching if o["match_fail"]),
            "gps_live": sum(1 for o in self.orders.values() if o["status"] == "IN_TRANSIT" and self._gps_live(o)),
            "gps_received": getattr(self, "gps_received", 0),
            "deviated": sum(1 for o in self.orders.values() if o["status"] == "IN_TRANSIT" and o["deviated"]),
            "delayed": sum(1 for o in self.orders.values() if o["status"] == "IN_TRANSIT" and o["delay_minutes"] > 30),
            "blacklisted": sum(1 for d in self.drivers.values() if d.get("blacklisted")),
            "surge_regions": sum(1 for m in self.market_board() if m["surge_on"]),
        }

    def anomaly_view(self, a: dict) -> dict:
        d = self.drivers.get(a["driver_id"], {})
        order = self.orders.get(a["order_id"]) if a["order_id"] else None
        return {
            **{k: a[k] for k in ("id", "type", "severity", "status", "text", "detail", "location", "order_id")},
            "type_label": ANOMALY_TYPE_LABEL[a["type"]], "at": _iso(a["at"]),
            "resolved_at": _iso(a["resolved_at"]), "driver": d.get("name", "-"), "plate": d.get("plate", "-"),
            "phone": d.get("phone", "-"),
            "temp_now": order["temp_now"] if order else None, "temp_set": order["temp_set"] if order else None,
        }

    def list_anomalies(self, type_: str = "ALL", status: str = "ALL") -> list[dict]:
        rows = [a for a in self.anomalies.values()
                if (type_ == "ALL" or a["type"] == type_) and (status == "ALL" or a["status"] == status)]
        rows.sort(key=lambda a: (a["status"] != "UNSOLVED", -a["at"].timestamp()))
        return [self.anomaly_view(a) for a in rows]

    def resolve_anomaly(self, anomaly_id: str) -> dict:
        a = self.anomalies.get(anomaly_id)
        if not a:
            raise ApiError(404, "이상 감지 로그를 찾을 수 없습니다.")
        if a["status"] != "UNSOLVED":
            raise ApiError(409, "이미 조치 완료된 건입니다.")
        a["status"], a["resolved_at"] = "RESOLVED", datetime.now()
        o = self.orders.get(a["order_id"]) if a["order_id"] else None
        if o and a["type"] == "STAGNATION":
            o["speed"] = 78
        if o and a["type"] == "COLD":
            o["temp_now"] = o["temp_set"]
        if o and a["type"] == "DEVIATION":
            o["deviated"] = False
            st = self.tracking.get(o["id"])
            if st:
                st["off"] = 0
        from ..engines.fsm.order_fsm import audit_entry
        audit_entry(self, o, "tracking", "resolve", a["type"], "RESOLVED", "관제",
                    note=f"{a['id']} {a['text']} 조치 완료", order_id=a["order_id"] or "-")
        return self.anomaly_view(a)
