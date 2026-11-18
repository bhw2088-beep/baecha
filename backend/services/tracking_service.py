"""
Phase 3: GPS 핑 처리 서비스 — 불법 재위탁 방지 · 경로 이탈(디바운싱) · 지연 감지

  기사 단말 / 가상 기사 ──POST /tracking/ping──▶ FastAPI (배정 기사 대조) ──XADD──▶ Redis Stream gps:stream
                                                    ──XREADGROUP──▶ gps_stream_worker ──▶ ingest_ping()

ingest_ping (핑 한 건)
  1) driver_id 대조: 배정 기사가 아닌 단말의 핑이면 반영하지 않고 '불법 재위탁 의심' 이상 감지 + 감사 로그
  2) engines/tracking_engine.locate: 계획 경로까지 최단 거리 · 경로상 진행률 (Shapely)
  3) 디바운싱: 기준 거리 초과가 연속 3번이면 정확히 3번째에 '경로 이탈' 경고 (GPS 튀김 오탐 방지)
  4) 진행률은 서버가 위경도로 직접 계산 (경로 안에 있을 때만 앞으로)
  5) Delay Delta > 30분이면 '도착 지연' 알림
감지 이벤트는 모두 SHA-256 해시 체인 감사 로그에 남는다.

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

from datetime import datetime

from ..config import TIME_SCALE, settings
from ..core.errors import ApiError
from ..engines import tracking_engine as te
from ..engines.fsm.order_fsm import audit_entry
from ..engines.matcher import spatial as geo
from ..models.order import _iso


def _near(lat: float, lon: float) -> str:
    key = min(geo.REGION_COORDS, key=lambda k: geo.haversine_km((lat, lon), geo.REGION_COORDS[k]))
    return f"{key} 부근 ({lat:.4f}, {lon:.4f})"


class TrackingMixin:
    # ------------------------------------------------------------------ 공차 기사 실시간 위치 (텔레메트리)
    def report_location(self, driver_id: str, lat: float, lon: float, speed: float = 0) -> str:
        """공차(운행 중 아님) 기사의 현재 위치 보고 → 매칭 거리 · PostGIS 반경 · 관제 지도에 반영.
        운행 중이면 무시 ('in_run' — 운행 위치는 GPS 핑 /tracking/ping 이 담당). 결과: 'ok' · 'in_run' · 'unknown'."""
        import time as _time
        d = self.drivers.get(driver_id)
        if not d:
            return "unknown"
        if self._active_run_of(driver_id):
            return "in_run"
        d["live"] = {"lat": round(lat, 6), "lon": round(lon, 6), "speed": round(speed, 1), "t": round(_time.time(), 1)}
        return "ok"

    # ------------------------------------------------------------------ 입구: 핑 검증 (API)
    def check_ping(self, order_id: str, driver_id: str | None, source: str = "driver_app") -> dict:
        """POST /tracking/ping 앞단 검증. 배정 기사가 아니면 재위탁 의심으로 기록하고 403."""
        o = self.orders.get(order_id)
        if not o:
            raise ApiError(404, "화물을 찾을 수 없습니다.")
        if o["status"] != "IN_TRANSIT":
            raise ApiError(409, "운행 중인 화물이 아닙니다.")
        if not driver_id or driver_id != o["driver_id"]:
            self._flag_reoutsource(o, driver_id, source)
            raise ApiError(403, "배정된 기사의 단말이 아닙니다. 불법 재위탁 의심으로 관제에 보고되었습니다.")
        return o

    def _flag_reoutsource(self, o: dict, driver_id: str | None, source: str):
        now = datetime.now()
        st = self._track(o)
        st["foreign_pings"] = st.get("foreign_pings", 0) + 1
        if self._open_anomaly(o["id"], "SUSPICIOUS_REOUTSOURCING"):
            return
        assigned = self.drivers.get(o["driver_id"], {})
        other = self.drivers.get(driver_id or "", {})
        self._new_anomaly(o, "SUSPICIOUS_REOUTSOURCING", "HIGH", "배정 기사가 아닌 단말의 위치 보고 (재위탁 의심)",
                          f"배정 {o['driver_id']} {assigned.get('name', '')} · 보고 {driver_id or '미상'} "
                          f"{other.get('name', '')} · {source}", "-", now, driver_id=o["driver_id"])
        audit_entry(self, o, "tracking", "reoutsource", None, "SUSPICIOUS", f"단말 {driver_id or '미상'}",
                    note=f"배정 기사 {o['driver_id']} 와 다른 단말의 GPS 핑 ({source})", at=now)

    # ------------------------------------------------------------------ 처리: 핑 한 건 반영 (스트림 워커)
    def apply_gps(self, msg: dict) -> bool:
        """Redis Streams 워커가 부르는 이름 (예전 이름 유지) → ingest_ping."""
        return self.ingest_ping(msg)["applied"]

    def _track(self, o: dict) -> dict:
        st = self.tracking.get(o["id"])
        if st is None:
            remaining = o["distance_km"] * (1 - o["progress"] / 100)
            st = self.tracking[o["id"]] = {
                "anchor": datetime.now(), "anchor_remaining_min": remaining / te.PLAN_SPEED_KMH * 60,
                "base_delay": o["delay_minutes"], "off": 0, "pings": 0, "alerts": 0, "max_km": 0.0,
                "delay_alerted": bool(self._open_anomaly(o["id"], "DELAY")), "foreign_pings": 0,
            }
        return st

    def ingest_ping(self, msg: dict) -> dict:
        o = self.orders.get(msg.get("order_id"))
        if not o or o["status"] != "IN_TRANSIT":
            return {"applied": False, "reason": "운행 중이 아님"}
        driver_id = msg.get("driver_id") or None
        if driver_id and driver_id != o["driver_id"]:
            self._flag_reoutsource(o, driver_id, msg.get("source", "stream"))
            return {"applied": False, "reason": "배정 기사 아님"}
        now = datetime.now()
        lat, lon = float(msg["lat"]), float(msg["lon"])
        st = self._track(o)
        st["pings"] += 1
        loc = te.locate((lat, lon), self._path(o))
        off = loc["distance_km"] > settings.deviation_km
        st["off"], event = te.debounce(st["off"], off, settings.deviation_hits)
        st["max_km"] = max(st["max_km"], loc["distance_km"])
        if event == "ALERT":
            st["alerts"] += 1
            o["deviated"] = True
            if not self._open_anomaly(o["id"], "DEVIATION"):
                self._new_anomaly(o, "DEVIATION", "MEDIUM",
                                  f"계획 경로 이탈 ({loc['distance_km']:.1f}km · {settings.deviation_hits}회 연속)",
                                  f"기준 {settings.deviation_km:g}km 초과 연속 {settings.deviation_hits}회 · "
                                  f"디바운싱 통과", _near(lat, lon), now)
            audit_entry(self, o, "tracking", "deviation", None, "DEVIATED", self._actor("driver", o["driver_id"]),
                        note=f"경로까지 {loc['distance_km']:.2f}km · {settings.deviation_hits}회 연속", at=now)
        elif event == "RECOVERED":
            o["deviated"] = False

        if self._open_anomaly(o["id"], "STAGNATION"):
            o["speed"] = 0
        else:
            o["speed"] = int(float(msg.get("speed") or 0))
            if not off:  # 경로 위에 있을 때만 서버가 계산한 진행률로 앞으로
                o["progress"] = max(o["progress"], min(97.0, loc["progress"] * 100))
        o["gps"] = {"lat": lat, "lon": lon, "ts": msg.get("ts"), "dist_km": loc["distance_km"],
                    "off_count": st["off"], "engine": loc["engine"], "source": msg.get("source")}
        o["gps_at"] = now
        self.gps_received = getattr(self, "gps_received", 0) + 1

        # 지연: 계획 기준 시점 이후 흐른 운행 시간(배속 반영) + 남은 거리 ÷ 계획 속도 − 계획 남은 시간
        elapsed = (now - st["anchor"]).total_seconds() * TIME_SCALE / 60
        remaining = o["distance_km"] * (1 - o["progress"] / 100)
        delta = st["base_delay"] + te.delay_minutes(elapsed, remaining, st["anchor_remaining_min"])
        o["delay_minutes"] = max(0, delta)
        if delta > settings.delay_alert_min and not st["delay_alerted"] \
                and not self._open_anomaly(o["id"], "STAGNATION"):
            st["delay_alerted"] = True
            self._new_anomaly(o, "DELAY", "MEDIUM", f"도착 지연 예상 (+{delta}분)",
                              f"계획 ETA 대비 Delay Delta {delta}분 > {settings.delay_alert_min}분",
                              _near(lat, lon), now)
            audit_entry(self, o, "tracking", "delay", None, f"+{delta}분", "추적 엔진",
                        note=f"계획 대비 {delta}분 지연", at=now)
        return {"applied": True, "distance_km": loc["distance_km"], "off_count": st["off"], "event": event,
                "progress": round(o["progress"], 2), "delay_minutes": o["delay_minutes"]}

    def _new_anomaly(self, o: dict, type_: str, severity: str, text: str, detail: str, location: str,
                     at: datetime, driver_id: str | None = None) -> dict:
        aid = self._next_id("LOG", at)
        a = {"id": aid, "order_id": o["id"], "driver_id": driver_id or o["driver_id"], "type": type_,
             "severity": severity, "status": "UNSOLVED", "at": at, "text": text, "detail": detail,
             "location": location, "resolved_at": None, "auto": True}
        self.anomalies[aid] = a
        return a

    # ------------------------------------------------------------------ 관제 화면
    def tracking_status(self) -> dict:
        rows = []
        for o in self.orders.values():
            if o["status"] != "IN_TRANSIT":
                continue
            st = self.tracking.get(o["id"], {})
            g = o.get("gps") or {}
            d = self.drivers.get(o["driver_id"], {})
            rows.append({"order_id": o["id"], "title": o["title"], "driver": d.get("name"), "plate": d.get("plate"),
                         "live": self._gps_live(o), "pings": st.get("pings", 0), "dist_km": g.get("dist_km"),
                         "off_count": st.get("off", 0), "alerts": st.get("alerts", 0),
                         "max_km": round(st.get("max_km", 0.0), 2), "delay_minutes": o["delay_minutes"],
                         "deviated": o["deviated"], "foreign_pings": st.get("foreign_pings", 0),
                         "gps_at": _iso(o.get("gps_at")), "source": g.get("source")})
        rows.sort(key=lambda r: (-r["off_count"], -(r["delay_minutes"] or 0)))
        return {"engine": "Shapely" if te.HAS_SHAPELY else "순수 파이썬 (Shapely 없음)",
                "rule": {"deviation_km": settings.deviation_km, "hits": settings.deviation_hits,
                         "delay_alert_min": settings.delay_alert_min},
                "runs": rows}
