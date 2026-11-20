"""
Phase 4: 화주-기사 상호 리뷰 (별점 1~5 + 태그형 평가) · 평점 · 블랙리스트

- 운송이 끝난 화물(DELIVERED · SETTLED)에 한해, 화주는 기사를 · 기사는 화주를 한 번씩 평가한다.
- 기사 평점은 매칭 2단계 LightGBM 피처(rating)로 바로 쓰인다.
- 블랙리스트: 리뷰 3건 이상 & 평균 3.0 미만이면 자동 이용 제한 → 매칭 1단계 하드 필터에서 제외.
  관제가 직접 제한 · 해제할 수도 있다 (사유 필수 · 감사 로그에 남김).

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

from datetime import datetime

from src.utils.errors import ApiError
from src.orm.models.order import DONE, _iso

DRIVER_TAGS = {  # 화주 → 기사
    "good": ["시간 엄수", "친절", "안전 운행", "화물 상태 양호", "소통 원활"],
    "bad": ["지연 도착", "화물 파손", "연락 두절", "불친절", "경로 이탈"],
}
SHIPPER_TAGS = {  # 기사 → 화주
    "good": ["상하차 신속", "대기 짧음", "정산 빠름", "정보 정확"],
    "bad": ["상하차 대기 지연", "작업 환경 불량", "화물 정보 불일치", "정산 지연"],
}
BLACKLIST_MIN_REVIEWS = 3
BLACKLIST_AVG = 3.0
DEFAULT_RATING = 4.5  # 리뷰가 없는 기사 (Cold-Start)


class ReviewMixin:
    # ------------------------------------------------------------------ 평점
    def _reviews_to(self, role: str, target_id: str) -> list[dict]:
        return [r for r in self.reviews.values() if r["to_role"] == role and r["to_id"] == target_id]

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        tags: dict[str, int] = {}
        for r in rows:
            for t in r["tags"]:
                tags[t] = tags.get(t, 0) + 1
        avg = round(sum(r["stars"] for r in rows) / len(rows), 2) if rows else None
        return {"avg": avg, "count": len(rows),
                "top_tags": [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:4]]}

    def driver_rating(self, driver_id: str) -> dict:
        return self._summary(self._reviews_to("driver", driver_id))

    def shipper_rating(self, shipper_id: str) -> dict:
        return self._summary(self._reviews_to("shipper", shipper_id))

    # ------------------------------------------------------------------ 작성
    def create_review(self, user: dict, order_id: str, stars: int, tags: list[str], comment: str = "") -> dict:
        o = self._get_order(order_id)
        if o["status"] not in DONE:
            raise ApiError(409, "운송이 끝난 화물만 평가할 수 있습니다.")
        if user["role"] == "shipper":
            if o["shipper_id"] != user["org_id"]:
                raise ApiError(403, "내 화물이 아닙니다.")
            from_id, to_role, to_id, allowed = user["org_id"], "driver", o["driver_id"], DRIVER_TAGS
        elif user["role"] == "driver":
            if o["driver_id"] != user["org_id"]:
                raise ApiError(403, "내 운송 건이 아닙니다.")
            from_id, to_role, to_id, allowed = user["org_id"], "shipper", o["shipper_id"], SHIPPER_TAGS
        else:
            raise ApiError(403, "화주와 차주만 평가할 수 있습니다.")
        if not 1 <= stars <= 5:
            raise ApiError(400, "별점은 1~5점입니다.")
        bad = [t for t in tags if t not in allowed["good"] + allowed["bad"]]
        if bad:
            raise ApiError(400, f"선택할 수 없는 태그입니다: {', '.join(bad)}")
        rid = f"REV-{order_id}-{user['role'][0].upper()}"
        if rid in self.reviews:
            raise ApiError(409, "이미 평가한 화물입니다.")
        now = datetime.now()
        self.reviews[rid] = {"id": rid, "order_id": order_id, "from_role": user["role"], "from_id": from_id,
                             "to_role": to_role, "to_id": to_id, "stars": int(stars), "tags": list(tags),
                             "comment": comment.strip()[:200], "at": now}
        if to_role == "driver":
            self._auto_blacklist(to_id, now)
        return self.review_view(self.reviews[rid])

    def _auto_blacklist(self, driver_id: str, now: datetime):
        d = self.drivers.get(driver_id)
        if not d or d.get("blacklisted"):
            return
        r = self.driver_rating(driver_id)
        if r["count"] >= BLACKLIST_MIN_REVIEWS and r["avg"] < BLACKLIST_AVG:
            d["blacklisted"] = True
            d["blacklist"] = {"at": _iso(now), "by": "자동 (평점 기준)",
                              "reason": f"리뷰 {r['count']}건 평균 {r['avg']}점 < {BLACKLIST_AVG}점",
                              "auto": True}

    # ------------------------------------------------------------------ 관제
    def set_blacklist(self, driver_id: str, on: bool, reason: str, by: str) -> dict:
        d = self.drivers.get(driver_id)
        if not d:
            raise ApiError(404, "기사를 찾을 수 없습니다.")
        if not reason.strip():
            raise ApiError(400, "사유를 입력해 주세요.")
        d["blacklisted"] = on
        d["blacklist"] = {"at": _iso(datetime.now()), "by": by, "reason": reason.strip(), "auto": False,
                          "action": "이용 제한" if on else "제한 해제"}
        from src.engines.fsm.order_fsm import audit_entry
        audit_entry(self, None, "compliance", "blacklist" if on else "unblacklist", None,
                    "BLACKLISTED" if on else "ACTIVE", by, note=f"{driver_id} {d['name']} · {reason.strip()}",
                    order_id=driver_id)
        if on:  # 진행 중인 제안은 바로 회수
            for o in self.orders.values():
                if o["status"] == "MATCHING" and o["offer"] and o["offer"]["driver_id"] == driver_id:
                    self._log(o, "cancelled", driver_id, datetime.now(), note="관제 이용 제한")
                    o["offer"] = None
        self._matching_tick()
        return {"driver_id": driver_id, "blacklisted": on, **d["blacklist"], "rating": self.driver_rating(driver_id)}

    def review_view(self, r: dict) -> dict:
        if r["to_role"] == "driver":
            target = self.drivers.get(r["to_id"], {}).get("name", r["to_id"])
        else:
            target = self.shippers.get(r["to_id"], {}).get("name", r["to_id"])
        if r["from_role"] == "driver":
            writer = self.drivers.get(r["from_id"], {}).get("name", r["from_id"])
        else:
            writer = self.shippers.get(r["from_id"], {}).get("name", r["from_id"])
        return {**r, "at": _iso(r["at"]), "target": target, "writer": writer}

    def list_reviews(self, role: str | None = None, target_id: str | None = None) -> list[dict]:
        rows = [r for r in self.reviews.values()
                if (role is None or r["to_role"] == role) and (target_id is None or r["to_id"] == target_id)]
        return [self.review_view(r) for r in sorted(rows, key=lambda r: r["at"], reverse=True)]

    def reviewable(self, user: dict) -> list[dict]:
        """내가 아직 평가하지 않은 완료 화물 (화주 · 차주 화면)."""
        key = "shipper_id" if user["role"] == "shipper" else "driver_id"
        out = []
        for o in sorted(self.orders.values(), key=lambda o: o["delivered_at"] or o["created_at"], reverse=True):
            if o["status"] in DONE and o[key] == user["org_id"] and o["driver_id"] \
                    and f"REV-{o['id']}-{user['role'][0].upper()}" not in self.reviews:
                other = (self.drivers.get(o["driver_id"], {}).get("name") if user["role"] == "shipper"
                         else self.shippers.get(o["shipper_id"], {}).get("name"))
                out.append({"order_id": o["id"], "title": o["title"], "other": other,
                            "delivered_at": _iso(o["delivered_at"])})
        return out

    def reputation_board(self) -> list[dict]:
        """관제 리뷰 탭: 기사별 평점 · 태그 · 블랙리스트 상태 (낮은 평점 먼저)."""
        rows = []
        for d in self.drivers.values():
            r = self.driver_rating(d["id"])
            rows.append({"id": d["id"], "name": d["name"], "plate": d["plate"], "truck_type": d["truck_type"],
                         **r, "blacklisted": bool(d.get("blacklisted")), "blacklist": d.get("blacklist"),
                         "at_risk": r["count"] >= BLACKLIST_MIN_REVIEWS - 1 and (r["avg"] or 5) < BLACKLIST_AVG + 0.5})
        return sorted(rows, key=lambda x: (not x["blacklisted"], x["avg"] if x["avg"] is not None else 9))

    def _seed_reviews(self, seed_reviews: list[dict], now: datetime):
        """시드 리뷰: 완료 화물 + 과거 운행 기록(HIST-...) 평가. 평점 낮은 기사 1명은 자동 블랙리스트 시연용."""
        from datetime import timedelta
        for i, r in enumerate(seed_reviews):
            r = dict(r)
            days = r.pop("days_ago", 3 + i)
            rid = f"REV-{r['order_id']}-{r['from_role'][0].upper()}"
            self.reviews[rid] = {"id": rid, "comment": "", **r, "at": now - timedelta(days=days)}
        for d in self.drivers.values():
            self._auto_blacklist(d["id"], now)
