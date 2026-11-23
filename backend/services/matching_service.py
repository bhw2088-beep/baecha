"""
배차 매칭 서비스 (메인 기능) — 3단계 하이브리드 매칭

  ① 공간 필터  (engines/matcher/spatial.py)  PostGIS ST_DWithin 으로 상차지 반경 안의 기사만
  ② 스코어링   기사 차량 기준 3D 적재 시뮬레이션(전량 적재 + 필수 제약) 통과 차량에 대해
              규칙 점수 = 거리 w_dist + 적재율 (1 − w_dist)
              + LightGBM 수락 예측 (engines/matcher/ml_ranker.py · 가상 데이터 학습)
              최종 점수 = 규칙 70% + ML 30%  (config.ML_WEIGHT)
  ③ 최적 배정  (engines/matcher/hungarian.py)  대기 화물 전체 × 빈 기사 전체를 헝가리안으로 1:1 배정
              → 배정된 기사에게 배차 제안 (Redis 분산 락 안에서 실행)
  제안 : settings.timeout_sec 안에 수락하면 배차 확정 / 거절·시간 초과면 다시 배정 라운드
  후보 없음 : 관제 '매칭 실패' (원인 표시) → 보조금 재공고 · 재매칭 · 기사 직접 지정
  AI 추천 리스트 : 기사가 '내 차량에 전량 실리는 매칭 대기 화물'을 점수 순으로 보고 직접 수락

Store 에 섞어 쓰는 Mixin 이다 (데이터는 core/database.py 의 Store 가 가진다).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from statistics import mean

from ..config import DEFAULT_SETTINGS, ML_THRESHOLD, ML_WEIGHT, SETTING_LABEL
from ..core.errors import ApiError
from ..core.redis_client import lock as redis_lock
from ..engines import compliance_engine as comp
from ..engines import kpi as kpi_engine
from ..engines import pricing_engine
from ..engines.matcher import hungarian, ml_ranker
from ..engines.matcher.cf_score import cf_score
from ..engines.matcher import spatial as geo
from ..models.order import MATCH_BY_LABEL, MATCH_EVENT_LABEL, _iso
from . import billing_service as pricing


def _tons(truck_type: str) -> float:
    return comp.tonnage(truck_type)  # '2.5톤 카고' → 2.5


def is_compatible(driver_truck: str, cargo_truck: str, cold_chain: bool) -> bool:
    """적재 계획(품목)이 없는 화물에만 쓰는 간이 판정: 냉장 조건 + 요청 톤수의 1~2.5배 차량."""
    if cold_chain and driver_truck not in pricing.COLD_TYPES:
        return False
    d, c = _tons(driver_truck), _tons(cargo_truck)
    return c <= d <= c * 2.5


def match_score(pickup_km: float, util: float, radius_km: float = 50, w_dist: float = 0.6) -> float:
    """0~1. 가까울수록(반경 대비), 적재함을 많이 채울수록 높다."""
    dist = max(0.0, 1 - pickup_km / radius_km)
    return round(w_dist * dist + (1 - w_dist) * max(0.0, min(1.0, util)), 4)


class MatchingMixin:
    # ------------------------------------------------------------------ 자동 매칭 엔진
    def _score(self, pickup_km: float, util: float) -> float:
        return match_score(pickup_km, util, self.settings["radius_km"], self.settings["w_dist"])

    def settings_view(self) -> dict:
        s = self.settings
        return {**s, "w_util": round(1 - s["w_dist"], 2), "defaults": DEFAULT_SETTINGS,
                "formula": f"거리 {s['w_dist'] * 100:.0f}% + 적재율 {(1 - s['w_dist']) * 100:.0f}%",
                "ml_weight": ML_WEIGHT, "ml_threshold": ML_THRESHOLD,
                "pipeline": (f"① PostGIS 반경 {s['radius_km']}km · 승인 · 노란 번호판 · 톤수 · 블랙리스트 필터 → "
                             f"② 규칙 점수(거리 {s['w_dist'] * 100:.0f}% + 적재율 {(1 - s['w_dist']) * 100:.0f}%) "
                             f"{100 - ML_WEIGHT * 100:.0f}% + AI 수락 예측 {ML_WEIGHT * 100:.0f}% "
                             f"(lgb_score ≥ {ML_THRESHOLD:.2f}) → ③ 헝가리안 최적 배정"),
                "log": self.settings_log[-8:][::-1]}

    def update_settings(self, new: dict, by: str) -> dict:
        """관제 설정 변경 → 매칭 실패 화물부터 새 기준으로 바로 다시 매칭 (진행 중인 제안은 그대로 둔다)."""
        new = {**new, "w_dist": round(new["w_dist"], 2)}
        changes = []
        for k, (label, unit) in SETTING_LABEL.items():
            if new[k] != self.settings[k]:
                fmt = (lambda v: f"{v * 100:.0f}%") if k == "w_dist" else (lambda v: f"{v}{unit}")  # noqa: B023 — 같은 반복에서 바로 씀
                changes.append(f"{label} {fmt(self.settings[k])} → {fmt(new[k])}")
        if not changes:
            return self.settings_view()
        self.settings.update(new)
        now = datetime.now()
        before = sum(1 for o in self.orders.values() if o["status"] == "MATCHING" and o["match_fail"])
        self._matching_tick()
        after = sum(1 for o in self.orders.values() if o["status"] == "MATCHING" and o["match_fail"])
        self.settings_log.append({"at": _iso(now), "by": by, "changes": changes,
                                  "note": f"매칭 실패 {before}건 → {after}건"})
        return self.settings_view()

    def _log(self, o: dict, event: str, driver_id: str | None, at: datetime, note: str = ""):
        d = self.drivers.get(driver_id) if driver_id else None
        o["match_log"].append({"at": _iso(at), "event": event, "label": MATCH_EVENT_LABEL[event],
                               "driver_id": driver_id, "driver": d["name"] if d else None, "note": note})

    @staticmethod
    def _offer_count(o: dict) -> int:
        return sum(1 for e in o["match_log"] if e["event"] == "offered")

    def _pending_offer_of(self, driver_id: str) -> dict | None:
        idx = getattr(self, "_driver_index", None)   # 매칭 라운드 중이면 미리 정리한 색인 사용
        if idx is not None:
            return idx["offer"].get(driver_id)
        return next((o for o in self.orders.values()
                     if o["status"] == "MATCHING" and o["offer"] and o["offer"]["driver_id"] == driver_id), None)

    def _driver_busy(self, driver_id: str, except_order: str | None = None) -> bool:
        """운행 중이거나 다른 화물의 제안에 답하는 중이면 새 제안을 받을 수 없다."""
        if self._active_run_of(driver_id):
            return True
        pending = self._pending_offer_of(driver_id)
        return pending is not None and pending["id"] != except_order

    def _fit(self, o: dict, truck_type: str) -> dict:
        """그 차종에 이 화물이 전량 · 필수 제약 충족으로 실리는지 + 적재율."""
        if not o["packing"]:
            ok = is_compatible(truck_type, o["truck_type"], o["cold_chain"])
            util = min(1.0, _tons(o["truck_type"]) / max(_tons(truck_type), 1)) if ok else 0.0
            return {"fits": ok, "volume_util": None, "weight_util": None, "util": util,
                    "fit_note": "차종 규격 기준 (적재 계획 없음)" if ok else "차량 규격 불일치"}
        res = self._plan_for_truck(o, truck_type)
        failed = [c["name"] for c in res["constraints"] if c["status"] == "fail"]
        unplaced = len(res.get("unplaced", []))  # 등록 시 저장된 계획은 전량 적재가 보장되어 키가 없음
        m = res["metrics"]
        fits = not failed and unplaced == 0
        note = "전량 적재" if fits else (f"{unplaced}개 미적재" if unplaced else "제약 위반: " + ", ".join(failed))
        return {"fits": fits, "volume_util": m["volume_util"], "weight_util": m["weight_util"],
                "util": max(m["volume_util"], m["weight_util"]), "fit_note": note}

    @staticmethod
    def _required_tonnage(o: dict) -> float:
        """화물 필수 최소 톤수: 적재 계획이 있으면 안전율 적용 총중량 · 총부피로 산출, 없으면 요청 차종 톤수."""
        if o.get("required_tonnage") is None:
            from ..config import settings
            from ..engines.packer.trucks import TRUCKS
            from ..engines.validation_engine import required_tonnage
            pk = o["packing"]
            if pk:
                m = pk["metrics"]
                need = required_tonnage(m["loaded_weight"], m["loaded_volume_m3"] * 1e9, list(TRUCKS.values()),
                                        settings.safety_margin, o["cold_chain"])
            else:
                need = _tons(o["truck_type"])
            o["required_tonnage"] = need or _tons(o["truck_type"])
        return o["required_tonnage"]

    # ---- 2단계 점수: 규칙 점수(거리 · 적재율) + LightGBM 수락 예측
    def _accept_rates(self) -> dict[str, float]:
        """기사별 과거 제안 수락률 (ML 피처). 매칭 라운드 동안은 한 번 계산한 값을 쓴다."""
        cached = getattr(self, "_rates_cache", None)
        if cached is not None:
            return cached
        # 매칭 기록을 한 번만 훑어 기사별 응답 · 수락 수를 센다 (기사마다 전체를 다시 훑지 않음)
        answered: dict[str, int] = {}
        accepted: dict[str, int] = {}
        for o in self.orders.values():
            for e in o["match_log"]:
                if e["event"] in ("accepted", "declined", "expired"):
                    did = e["driver_id"]
                    answered[did] = answered.get(did, 0) + 1
                    accepted[did] = accepted.get(did, 0) + (e["event"] == "accepted")
        rates = {}
        for did in self.drivers:
            n = answered.get(did, 0)
            rates[did] = round(100 * accepted[did] / n, 1) / 100 if n else 0.7
        return rates

    def _all_region_history(self) -> dict[str, tuple[list[str], list[str]]]:
        """전체 기사의 수락 · 거절 상차 권역을 한 번에 (매칭 라운드 동안 재사용)."""
        out: dict[str, tuple[list[str], list[str]]] = {}
        for o in self.orders.values():
            region = None
            for e in o["match_log"]:
                kind = (0 if e["event"] in ("accepted", "picked", "assigned")
                        else 1 if e["event"] in ("declined", "expired") else None)
                if kind is None or not e["driver_id"]:
                    continue
                region = region or pricing_engine.region_of(o["origin"])
                out.setdefault(e["driver_id"], ([], []))[kind].append(region)
        return out

    def _region_history(self, driver_id: str) -> tuple[list[str], list[str]]:
        """기사가 수락 · 거절한 화물의 상차 권역 목록 (cf_score 입력)."""
        cached = getattr(self, "_region_cache", None)
        if cached is not None:
            acc, dec = cached.get(driver_id, ([], []))
            return list(acc), list(dec)
        acc, dec = [], []
        for o in self.orders.values():
            for e in o["match_log"]:
                if e["driver_id"] != driver_id:
                    continue
                if e["event"] in ("accepted", "picked", "assigned"):
                    acc.append(pricing_engine.region_of(o["origin"]))
                elif e["event"] in ("declined", "expired"):
                    dec.append(pricing_engine.region_of(o["origin"]))
        return acc, dec

    def _cf(self, o: dict, driver_id: str) -> float:
        d = self.drivers[driver_id]
        acc, dec = self._region_history(driver_id)
        return cf_score(d["location"], o["origin"], o["destination"], acc, dec)

    def _ml_features(self, o: dict, driver_id: str, km: float, util: float, rates: dict) -> dict:
        mk = self._fare_est(o).get("market") or {}
        return {"pickup_km": km, "util": util, "fare_gap": self._fare_est(o)["gap_ratio"],
                "trip_km": o["distance_km"], "cold": float(o["cold_chain"]), "urgent": float(o["urgent"]),
                "accept_rate": rates.get(driver_id, 0.7), "cf_score": self._cf(o, driver_id),
                "rating": self.driver_rating(driver_id)["avg"] or 4.5,
                "rush_hour": float(pricing_engine.is_rush_hour()),
                # 날씨 · 수급: 화물 등록 때 운임 산정에 쓴 상차 권역 값 (fare_est.market)
                "weather": float(mk.get("weather_multiplier", 0.0)), "ds_ratio": float(mk.get("ds_ratio", 1.0))}

    def _combine(self, rule: float, ml: float) -> float:
        return round((1 - ML_WEIGHT) * rule + ML_WEIGHT * ml, 4)

    def _pair_score(self, o: dict, driver_id: str, km: float, util: float) -> dict:
        """화물 × 기사 한 쌍의 점수 (추천 리스트 · 배차 확정 기록용)."""
        rule = self._score(km, util)
        feats = self._ml_features(o, driver_id, km, util, self._accept_rates())
        ml = ml_ranker.predict([feats])[0]
        return {"rule_score": rule, "ml_prob": ml, "score": self._combine(rule, ml), "cf_score": feats["cf_score"],
                "lgb_pass": ml >= ML_THRESHOLD}

    def _evaluate(self, o: dict) -> list[dict]:
        """등록된 모든 기사를 이 화물 기준으로 평가.
        ① PostGIS 반경 필터 → ② 도로거리 · 3D 적재 시뮬레이션 · 규칙 점수 + LightGBM 수락 예측."""
        origin = geo.geocode(o["origin"])
        radius = self.settings["radius_km"]
        near = geo.drivers_within(origin, radius) if getattr(self, "db", None) else None  # ① ST_DWithin
        rates = self._accept_rates()
        fit_cache: dict[str, dict] = {}
        rows = []
        need = self._required_tonnage(o)
        for d in self.drivers.values():
            if not self.driver_dispatchable(d):
                continue  # 하드 필터: 관제 승인 · 사업용 노란 번호판 · 블랙리스트 아님
            if d.get("tonnage", _tons(d["truck_type"])) < need:
                continue  # 하드 필터: 기사 톤수 ≥ 화물 필수 최소 톤수 (적재 검증 산출)
            t = d["truck_type"]
            if t not in fit_cache:
                fit_cache[t] = self._fit(o, t)
            f = fit_cache[t]
            km = round(geo.route_km(self._driver_coord(d), origin), 1)
            spatial_ok = near is None or d["id"] in near or self._active_run_of(d["id"]) is not None
            rows.append({
                **self.driver_brief(d["id"]), "location": d["location"], "pickup_km": km, **f,
                "busy": self._driver_busy(d["id"], except_order=o["id"]),
                "declined": d["id"] in o["declined"], "in_radius": spatial_ok and km <= radius,
                "rule_score": self._score(km, f["util"]) if f["fits"] else None, "ml_prob": None, "score": None,
                "cf_score": None, "lgb_pass": None,
            })
        fit_rows = [r for r in rows if r["fits"]]
        feats = [self._ml_features(o, r["id"], r["pickup_km"], r["util"], rates) for r in fit_rows]
        probs = ml_ranker.predict(feats)
        for r, p, f in zip(fit_rows, probs, feats):
            r["ml_prob"], r["score"] = p, self._combine(r["rule_score"], p)
            r["cf_score"], r["lgb_pass"] = f["cf_score"], p >= ML_THRESHOLD  # 보수적 임계값 (행동적 오탐 차단)
        return rows

    @staticmethod
    def _ranking(rows: list[dict]) -> list[dict]:
        """자동 제안 후보: 전량 적재 · 비어 있음 · 거절 안 함 · 반경 안 · lgb_score ≥ 임계값."""
        ok = [r for r in rows if r["fits"] and not r["busy"] and not r["declined"] and r["in_radius"]
              and r["lgb_pass"]]
        return sorted(ok, key=lambda r: (-r["score"], r["pickup_km"]))

    def _fail_reasons(self, o: dict, rows: list[dict]) -> list[str]:
        fit = [r for r in rows if r["fits"]]
        reasons = []
        if not fit:
            reasons.append("등록 차량 중 이 화물을 전량 적재할 수 있는 차량이 없음")
        else:
            free = [r for r in fit if not r["busy"] and not r["declined"]]
            if not free:
                reasons.append(f"적재 가능 차량 {len(fit)}대 모두 운행 중 · 다른 제안 대기 중 · 거절")
            elif not any(r["in_radius"] for r in free):
                nearest = min(r["pickup_km"] for r in free)
                reasons.append(f"반경 {self.settings['radius_km']}km 내 적재 가능한 공차 없음 "
                               f"(가장 가까운 공차 {nearest:.0f}km)")
            else:
                low = [r for r in free if r["in_radius"] and not r["lgb_pass"]]
                if low:
                    best = max(r["ml_prob"] for r in low)
                    reasons.append(f"반경 안 공차 {len(low)}대 모두 AI 수락 예측이 임계값 {ML_THRESHOLD:.2f} 미만 "
                                   f"(최고 {best:.2f}) → 보수적 차단")
        if o["declined"]:
            reasons.append(f"기사 {len(o['declined'])}명 거절 · 응답 없음")
        if o["match_note"]:
            reasons.append(o["match_note"])
        return reasons or ["제안할 수 있는 기사 없음"]

    def _run_matching(self, o: dict | None = None, now: datetime | None = None):
        """(이전 이름 유지) 매칭 라운드를 한 번 돌린다 — 화물 하나가 아니라 대기 화물 전체를 함께 배정."""
        self._matching_round(now)

    def _matching_round(self, now: datetime | None = None):
        """③ 헝가리안 최적 배정: 제안 중이 아닌 매칭 대기 화물 전체 × 비어 있는 기사 전체를 한 번에 1:1 배정.
        배정된 쌍에는 배차 제안을 보내고, 후보가 없는 화물은 실패 원인을 기록한다.
        여러 서버(워커)가 동시에 배정하지 않도록 Redis 분산 락 안에서 실행한다."""
        with redis_lock("matching") as got:
            if not got:
                return  # 다른 워커가 배정 중 → 다음 요청 때 다시
            now = now or datetime.now()
            pending = sorted((o for o in self.orders.values() if o["status"] == "MATCHING" and not o["offer"]),
                             key=lambda o: o["posted_at"])
            if not pending:
                return
            t0 = time.perf_counter()
            self._rates_cache = self._accept_rates()
            self._region_cache = self._all_region_history()
            self._driver_index = self._build_driver_index()   # 후보 평가 동안 화물 상태는 바뀌지 않는다
            try:
                evals = {o["id"]: self._evaluate(o) for o in pending}
            finally:
                self._rates_cache = None
                self._region_cache = None
                self._driver_index = None
            rankings = {oid: self._ranking(rows) for oid, rows in evals.items()}
            score = {(oid, r["id"]): r["score"] for oid, ranking in rankings.items() for r in ranking}
            order_ids = [o["id"] for o in pending]
            driver_ids = sorted({d for _, d in score})
            pairs = hungarian.assign(order_ids, driver_ids, score)
            latency_ms = (time.perf_counter() - t0) * 1000
            greedy = hungarian.greedy(order_ids, driver_ids, score)
            if score:
                self.last_round = {"at": _iso(now), "orders": len(pending), "drivers": len(driver_ids),
                                   "assigned": len(pairs), "total": round(sum(s for *_, s in pairs), 3),
                                   "greedy_assigned": len(greedy), "greedy_total": round(sum(s for *_, s in greedy), 3)}
            self._log_round(now, pending, evals, rankings, pairs, greedy, latency_ms)
            assigned = {oid: (did, s) for oid, did, s in pairs}
            for o in pending:
                ranking = rankings[o["id"]]
                if o["id"] in assigned:
                    did, s = assigned[o["id"]]
                    top = next(r for r in ranking if r["id"] == did)
                    rank = [r["id"] for r in ranking].index(did) + 1
                    o["match_fail"], o["match_fail_at"], o["match_wait"] = None, None, None
                    o["offer"] = {
                        "driver_id": did, "offered_at": now,
                        "expires_at": now + timedelta(seconds=self.settings["timeout_sec"]),
                        "timeout": self.settings["timeout_sec"],
                        "pickup_km": top["pickup_km"], "volume_util": top["volume_util"],
                        "weight_util": top["weight_util"], "util": top["util"], "score": s,
                        "rule_score": top["rule_score"], "ml_prob": top["ml_prob"], "rank": rank,
                        "candidates": len(ranking), "round": self._offer_count(o) + 1, "assign": "hungarian",
                    }
                    self._log(o, "offered", did, now,
                              note=f"헝가리안 최적 배정 · 후보 {len(ranking)}대 중 {rank}순위 · 점수 {s * 100:.0f} "
                                   f"(AI 수락 예측 {top['ml_prob'] * 100:.0f}%)")
                elif not ranking:
                    if o["match_fail"] is None:
                        o["match_fail_at"] = now
                    o["match_fail"], o["match_wait"] = self._fail_reasons(o, evals[o["id"]]), None
                else:
                    # 후보는 있지만, 전체 최적 배정에서 그 기사들이 다른 화물에 배정됨 → 다음 라운드 대기
                    o["match_fail"], o["match_fail_at"] = None, None
                    o["match_wait"] = f"후보 {len(ranking)}대가 모두 다른 화물에 최적 배정됨 → 다음 배정 대기"

    # ---- 매칭 성능 로그 (matching_metrics_logs)
    def _log_round(self, now, pending, evals, rankings, pairs, greedy, latency_ms: float):
        """배정 라운드 기록. 제안을 보낸 라운드는 항상, 아무 변화 없는 라운드는 30초에 한 번만 남긴다."""
        logs = self.metrics_log
        if not pairs and logs and (now - logs[-1]["at"]).total_seconds() < 30:
            return
        km = {(oid, r["id"]): r["pickup_km"] for oid, ranking in rankings.items() for r in ranking}
        hk = [km[(o, d)] for o, d, _ in pairs]
        gk = [km[(o, d)] for o, d, _ in greedy]
        h_avg, g_avg = (mean(hk) if hk else None), (mean(gk) if gk else None)
        # KPI 기준선: 같은 후보 안에서 '단순 최단거리 매칭'(등록 순서 · 가장 가까운 공차) 했을 때의 평균 공차 거리
        near = hungarian.nearest([o["id"] for o in pending], sorted({d for _, d in km}), km)
        nk = [k for *_, k in near]
        n_avg = mean(nk) if nk else None
        # 선택지가 있는 라운드만 비교: 어떤 화물에 후보가 2명 이상이거나, 어떤 기사가 화물 2건 이상의 후보일 때.
        # (화물 1건 × 기사 1명처럼 고를 게 없으면 어떤 방식이든 같은 기사 → 단축률 0% 가 평균을 왜곡)
        has_choice = len(km) > min(len({o for o, _ in km}), len({d for _, d in km})) if km else False
        reduction = (round((g_avg - h_avg) / g_avg * 100, 1)
                     if has_choice and h_avg is not None and g_avg else None)
        vs_near = round((n_avg - h_avg) / n_avg * 100, 1) if has_choice and h_avg is not None and n_avg else None
        filtered = sum(1 for rows in evals.values() for r in rows
                       if r["fits"] and not r["busy"] and r["in_radius"] and r["lgb_pass"] is False)
        self._metric_seq = getattr(self, "_metric_seq", len(logs)) + 1
        logs.append({"id": f"MR-{self._metric_seq:06d}", "at": now, "orders": len(pending),
                     "drivers": len({d for r in rankings.values() for d in (x["id"] for x in r)}),
                     "assigned": len(pairs), "greedy_assigned": len(greedy), "latency_ms": round(latency_ms, 2),
                     "hungarian_avg_km": round(h_avg, 2) if h_avg is not None else None,
                     "greedy_avg_km": round(g_avg, 2) if g_avg is not None else None,
                     "deadhead_reduction_pct": reduction, "threshold_filtered": filtered,
                     "nearest_assigned": len(near),
                     "nearest_avg_km": round(n_avg, 2) if n_avg is not None else None,
                     "deadhead_vs_nearest_pct": vs_near, "candidate_pairs": len(km), "has_choice": has_choice,
                     "failed": sum(1 for o in pending if not rankings[o["id"]])})
        del logs[:-500]  # 최근 500 라운드만 유지

    def matching_metrics(self) -> dict:
        """실시간 매칭 메트릭스: 배차 지연 p95 · 공차 거리 단축률 · 거절률 · 임계값 차단."""
        logs = self.metrics_log
        lat = sorted(e["latency_ms"] for e in logs)
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None
        # 공차 거리 비교는 선택지가 있던 라운드만 (예전 기록은 후보 기사 2명 이상이면 선택지가 있던 것으로 본다)
        comparable = [e for e in logs if e["assigned"] and e.get("has_choice", e.get("drivers", 0) >= 2)]
        red = [e["deadhead_reduction_pct"] for e in comparable if e["deadhead_reduction_pct"] is not None]
        # 단순 최단거리 기준선 대비: 라운드별 평균 상차 거리를 배정 건수로 가중해 합친다
        nr = [e for e in comparable if e.get("deadhead_vs_nearest_pct") is not None]
        h_km = sum(e["hungarian_avg_km"] * e["assigned"] for e in nr)
        n_km = sum(e["nearest_avg_km"] * e["nearest_assigned"] for e in nr)
        n_cnt, h_cnt = sum(e["nearest_assigned"] for e in nr), sum(e["assigned"] for e in nr)
        vs_near = round((n_km / n_cnt - h_km / h_cnt) / (n_km / n_cnt) * 100, 1) if n_cnt and h_cnt and n_km else None
        events = [e for o in self.orders.values() for e in o["match_log"]]
        answered = [e for e in events if e["event"] in ("accepted", "declined", "expired")]
        rejected = [e for e in answered if e["event"] != "accepted"]
        return {
            "rounds": len(logs),
            "dispatch_latency_p95_sec": round(p95 / 1000, 4) if p95 is not None else None,
            "dispatch_latency_p95_ms": p95,
            "target_p95_sec": 3.0,
            "empty_deadhead_km_reduction_pct": round(mean(red), 1) if red else None,
            "deadhead_vs_nearest_pct": vs_near,
            "target_deadhead_vs_nearest_pct": 10.0,
            "comparable_rounds": len(nr),          # 공차 거리 비교에 쓴 라운드 수 (선택지가 있던 라운드)
            "rejection_rate_pct": round(100 * len(rejected) / len(answered), 1) if answered else None,
            "threshold_filtered": sum(e["threshold_filtered"] for e in logs),
            "ml_threshold": ML_THRESHOLD,
            "recent": [{**e, "at": _iso(e["at"])} for e in logs[-15:][::-1]],
        }

    # ---- KPI 목표 대비 현황 (engines/kpi.py 의 목표 · 판정 기준)
    BENCHMARK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                                  "benchmark_metrics.json")

    LARGE_BENCHMARK_FILE = os.path.join(os.path.dirname(BENCHMARK_FILE), "benchmark_large.json")
    PACKING_BENCHMARK_FILE = os.path.join(os.path.dirname(BENCHMARK_FILE), "benchmark_packing.json")

    def _packing_benchmark(self) -> tuple[dict | None, dict | None]:
        """적재 전용 대량 스트레스 테스트 사본 (tests/stress/packing_stress.py · 무작위 화물 10,000건) → (전체, 현재 여유값 결과).
        적재 · 과적 KPI 의 측정 기준 — 지금 설정된 상부 허용하중 여유(top_load_margin)와 같은 값으로 잰 결과를 쓴다."""
        try:
            with open(self.PACKING_BENCHMARK_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None, None
        from ..config import settings as app_settings
        bm = d.get("by_margin") or {}
        key = next((k for k in bm if abs(float(k) - app_settings.top_load_margin) < 1e-6), None)
        return d, (bm[key] if key else None)

    def _benchmark(self, large: bool = False) -> dict | None:
        """스트레스 테스트 결과 사본 (backend/data).
        기본(시드 7 · 하루 118건): GPS 오알람처럼 실시간으로 정답을 알 수 없는 지표
        대규모(--preset 8k · 8,008건): 매칭 · 적재 KPI 의 측정 기준"""
        try:
            with open(self.LARGE_BENCHMARK_FILE if large else self.BENCHMARK_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def kpi_scorecard(self) -> dict:
        """구현 단계별 현실적 KPI 8개: 업계 표준 · 목표 · 현재 값 · 판정 · 근거."""
        m = self.matching_metrics()
        bench = self._benchmark()
        large = self._benchmark(large=True)
        lk = {r["key"]: r for r in (large or {}).get("kpi", [])}   # 대규모 실행의 KPI 판정값
        n_large = (large or {}).get("attempts")
        src_large = f"스트레스 테스트 {n_large:,}건" if n_large else None
        rows = []

        # 1) 가입 검증 자동화율: 화주 · 차주 가입 신청은 모두 규칙 엔진이 자동 판정하고 감사 로그에 남긴다
        sign = [e for e in self.audit if e.get("event") in ("signup_pass", "signup_reject")]
        passed = sum(1 for e in sign if e["event"] == "signup_pass")
        rows.append(kpi_engine.evaluate(
            "compliance_auto_pct", 100.0 if sign else None, source="실시간 (감사 로그)",
            basis=f"가입 신청 {len(sign)}건 자동 판정 · 통과 {passed} · 차단 {len(sign) - passed}" if sign
            else "아직 가입 신청 없음 (시드 계정 제외)"))

        # 2) 적재 오탐 · 과적: 배차 확정 화물을 '배정 차량' 기준으로 다시 계획해 필수 규칙 위반 여부 확인
        checked = violations = 0
        for o in self.orders.values():
            if not o.get("match") or not o.get("packing"):
                continue
            d = self.drivers.get(o["match"]["driver_id"])
            if not d:
                continue
            p = self._plan_for_truck(o, d["truck_type"])
            checked += 1
            violations += bool(p.get("unplaced") or any(c["status"] == "fail" for c in p["constraints"]))
        b_note = (f" · 스트레스 테스트 {bench['packing_checked']}건 중 위반 {bench['packing_violations']}건"
                  if bench and "packing_checked" in bench else "")
        bc = (bench or {}).get("bad_cargo")
        if bc:
            b_note += (f" · 위반 화물 주입 {sum(bc['injected'].values())}건 → 차단 {sum(bc['blocked'].values())}건 "
                       f"(놓침 {bc['missed']})")
        live_note = f"실시간 배차 확정 {checked}건 재검증 · 위반 {violations}건"
        pk, pm = self._packing_benchmark()
        if pm and pm.get("ok"):
            bad_b = sum(v["blocked"] for v in pm["bad"].values())
            bad_n = sum(v["injected"] for v in pm["bad"].values())
            real = (pm.get("real") or {}).get("-5:10") or next(iter((pm.get("real") or {}).values()), {})
            over = (f" · 실제 무게(신고 대비 −5 ~ +10%) 과적 {real['over']}건 "
                    f"({100 * real['over'] / real['checked']:.2f}%) · 현실 위험률 {real.get('rate')}%"
                    if real and real.get("checked") else "")
            rows.append(kpi_engine.evaluate(
                "packing_violation_pct", round(100 * pm["declared_fail"] / pm["ok"], 2),
                source=f"적재 스트레스 테스트 {pk.get('cases', 0):,}건",
                basis=f"무작위 정상 화물 {pm['clean']:,}건 중 등록 {pm['ok']:,}건 재검증 · 위반 {pm['declared_fail']}건 "
                      f"(신고 무게 기준 · 과적 0건) · 위반 화물 6종 {bad_n:,}건 → 차단 {bad_b:,}건{over} · "
                      f"참고: {live_note}"))
        elif "packing_violation_pct" in lk:
            bc_l = large.get("bad_cargo") or {}
            rows.append(kpi_engine.evaluate(
                "packing_violation_pct", lk["packing_violation_pct"]["value"], source=src_large,
                basis=f"배차 {large.get('packing_checked', 0):,}건 재검증 · 위반 {large.get('packing_violations', 0)}건 · "
                      f"위반 화물 주입 {sum((bc_l.get('injected') or {}).values()):,}건 → 차단 "
                      f"{sum((bc_l.get('blocked') or {}).values()):,}건 · 정상 화물 오차단 "
                      f"{bc_l.get('clean_blocked', 0)}/{bc_l.get('clean_checked', 0):,} · 참고: {live_note}"))
        else:
            rows.append(kpi_engine.evaluate(
                "packing_violation_pct", round(100 * violations / checked, 2) if checked else None,
                source="실시간 (배정 차량 재검증)", basis=f"{live_note}{b_note}"))

        # 3) 배차 지연 p95 · 4) 거절률 · 5) 단순 최단거리 대비 공차 거리 단축률 (매칭 라운드 로그)
        live_p95 = m["dispatch_latency_p95_sec"]
        if "dispatch_p95_sec" in lk:
            rows.append(kpi_engine.evaluate(
                "dispatch_p95_sec", lk["dispatch_p95_sec"]["value"], source=src_large,
                basis=f"배정 라운드 {large.get('rounds', 0):,}회 · 기사 {large.get('drivers', 0)}명"
                      + (f" · 참고: 실시간 {live_p95:.2f}초 ({m['rounds']}회)" if live_p95 is not None else "")))
        else:
            rows.append(kpi_engine.evaluate(
                "dispatch_p95_sec", live_p95, source="실시간 (매칭 라운드)",
                basis=f"배정 라운드 {m['rounds']}회 · 평가 + 헝가리안 배정 소요"))
        answered = sum(1 for o in self.orders.values() for e in o["match_log"]
                       if e["event"] in ("accepted", "declined", "expired"))
        if "rejection_rate_pct" in lk:
            rows.append(kpi_engine.evaluate(
                "rejection_rate_pct", lk["rejection_rate_pct"]["value"], source=src_large,
                basis=f"제안 {large.get('offers', 0):,}건 · 거절 {large.get('declined', 0):,}건"
                      + (f" · 참고: 실시간 {m['rejection_rate_pct']}% (응답 {answered}건)"
                         if m["rejection_rate_pct"] is not None else "")))
        else:
            rows.append(kpi_engine.evaluate(
                "rejection_rate_pct", m["rejection_rate_pct"], source="실시간 (제안 응답)",
                basis=f"응답 {answered}건 · 거절 + 무응답 비율"))
        nr = [e for e in m["recent"] if e.get("deadhead_vs_nearest_pct") is not None
              and e.get("has_choice", e.get("drivers", 0) >= 2)]
        last = nr[0] if nr else None
        vb = ((large or {}).get("detail") or {}).get("vs_baselines") or {}
        if "deadhead_vs_nearest_pct" in lk:
            rows.append(kpi_engine.evaluate(
                "deadhead_vs_nearest_pct", lk["deadhead_vs_nearest_pct"]["value"], source=src_large,
                basis=(f"헝가리안 {vb.get('hungarian_avg_km')}km vs 최단거리 {vb.get('nearest_avg_km')}km · "
                       f"선택지 있던 라운드 {vb.get('comparable_rounds')}/{vb.get('all_rounds')}")
                + (f" · 참고: 실시간 {m['deadhead_vs_nearest_pct']}%" if m["deadhead_vs_nearest_pct"] is not None else "")))
        else:
            rows.append(kpi_engine.evaluate(
                "deadhead_vs_nearest_pct", m["deadhead_vs_nearest_pct"], source="실시간 (매칭 라운드)",
                basis=(f"최근 라운드 헝가리안 {last['hungarian_avg_km']}km vs 최단거리 기준선 {last['nearest_avg_km']}km"
                       if last else "비교 가능한 배정 라운드 없음 (후보가 1명뿐인 라운드는 제외)")
                + f" · 선택지가 있던 라운드 {m['comparable_rounds']}회 · 같은 후보 기사 안에서 비교"))

        # 6) 경로 이탈 오알람률: 실시간으로는 '진짜 이탈'인지 정답이 없어 스트레스 테스트(정답 있는 시나리오)로 판정
        g = (bench or {}).get("gps")
        normal = (g["runs"] - g["deviation_runs"]) if g else 0
        live_alerts = sum(1 for a in self.anomalies.values() if a["type"] == "DEVIATION")
        rows.append(kpi_engine.evaluate(
            "gps_false_alarm_pct", round(100 * g["false_alarms"] / normal, 1) if g and normal else None,
            source="스트레스 테스트",
            basis=(f"정상 운행 {normal}회(단발 튐 {g['spikes']}회 포함) 오알람 {g['false_alarms']}건 · "
                   f"고의 이탈 {g['detected']}/{g['deviation_runs']} 감지" if g else "스트레스 테스트 결과 없음")
            + f" · 실시간 이탈 경보 {live_alerts}건"))

        # 7) 관제 반영 지연: Redis Streams 메시지 ID(XADD 시각) → 저장소 반영 시각
        from ..workers import gps_stream_worker
        rl = gps_stream_worker.relay_latency()
        rows.append(kpi_engine.evaluate(
            "relay_p95_sec", round(rl["p95_ms"] / 1000, 3) if rl["p95_ms"] is not None else None,
            source="실시간 (Redis Streams)",
            basis=(f"핑 {rl['count']:,}건 · 평균 {rl['avg_ms']:.0f}ms · 서버 반영 기준 (관제 화면은 3초마다 자동 갱신)"
                   if rl["count"] else "GPS 스트림 미수신 (Redis 없음 · 운행 중 화물 없음)")))

        # 8) PG 정산 성공률: 결제 시도 중 승인된 비율 (취소 · 환불은 승인 이후라 성공으로 셈)
        pays = self.payments()
        failed_pg = getattr(self, "pg_failures", 0)
        ok_pg = sum(1 for p in pays if p.get("status") in ("APPROVED", "CANCELLED"))
        tried = len(pays) + failed_pg
        rows.append(kpi_engine.evaluate(
            "pg_success_pct", round(100 * ok_pg / tried, 1) if tried else None, source="실시간 (모의 PG)",
            basis=f"결제 시도 {tried}건 · 승인 {ok_pg}건 · 모의 PG(실제 출금 없음)" if tried else "아직 결제 없음"))

        return {"rows": rows, "summary": kpi_engine.summary(rows),
                "benchmark_at": (bench or {}).get("generated_at"),
                "large_benchmark_at": (large or {}).get("generated_at"), "large_attempts": n_large,
                "packing_benchmark_at": (pk or {}).get("generated_at"), "packing_cases": (pk or {}).get("cases")}

    def _matching_tick(self):
        """제안 시간 초과 처리 → 제안이 없는 매칭 대기 화물(실패 포함) 전체를 헝가리안으로 다시 배정."""
        now = datetime.now()
        for o in self.orders.values():
            off = o["offer"] if o["status"] == "MATCHING" else None
            if off and now >= off["expires_at"]:
                o["declined"].append(off["driver_id"])
                self._log(o, "expired", off["driver_id"], now)
                o["offer"] = None
        self._matching_round(now)

    def _assign(self, o: dict, driver_id: str, by: str):
        now = datetime.now()
        d = self.drivers[driver_id]
        f = self._fit(o, d["truck_type"])
        km = round(geo.route_km(self._driver_coord(d), geo.geocode(o["origin"])), 1)
        ps = self._pair_score(o, driver_id, km, f["util"]) if f["fits"] else {}
        o["match"] = {"driver_id": driver_id, "pickup_km": km, "volume_util": f["volume_util"],
                      "weight_util": f["weight_util"], "util": f["util"],
                      "score": ps.get("score"), "rule_score": ps.get("rule_score"), "ml_prob": ps.get("ml_prob"),
                      "offers": self._offer_count(o), "by": by}
        self._log(o, {"auto": "accepted", "pick": "picked"}.get(by, "assigned"), driver_id, now)
        actor = "관제" if by == "admin" else self._actor("driver", driver_id)
        self._fsm(o, "assign", actor, note=f"{MATCH_BY_LABEL[by]} · {d['name']} ({d['truck_type']})", at=now)
        o.update(driver_id=driver_id, assigned_at=now, offer=None, match_fail=None, match_fail_at=None)

    def _matching_view(self, o: dict) -> dict | None:
        if o["status"] != "MATCHING":
            return None
        off = o["offer"]
        state = "OFFERED" if off else ("FAILED" if o["match_fail"] else "SEARCHING")
        v = {"state": state, "round": self._offer_count(o), "declined": len(o["declined"]),
             "reasons": o["match_fail"] or [], "wait_note": o.get("match_wait"),
             "radius_km": self.settings["radius_km"],
             "timeout": off["timeout"] if off else self.settings["timeout_sec"],
             "formula": self.settings_view()["formula"],
             "fail_seconds": int((datetime.now() - o["match_fail_at"]).total_seconds()) if o["match_fail_at"] else None}
        if off:
            v.update(driver=self.driver_brief(off["driver_id"]), pickup_km=off["pickup_km"],
                     volume_util=off["volume_util"], weight_util=off["weight_util"], util=off["util"],
                     score=off["score"], rule_score=off.get("rule_score"), ml_prob=off.get("ml_prob"),
                     rank=off.get("rank"), candidates=off["candidates"], offer_round=off["round"],
                     expires_in=max(0, int((off["expires_at"] - datetime.now()).total_seconds())))
        return v

    # ------------------------------------------------------------------ driver
    def _driver_offer_stats(self, driver_id: str) -> dict:
        events = [e for o in self.orders.values() for e in o["match_log"] if e["driver_id"] == driver_id]
        answered = [e for e in events if e["event"] in ("accepted", "declined", "expired")]
        accepted = sum(1 for e in answered if e["event"] == "accepted")
        return {"offers": sum(1 for e in events if e["event"] == "offered"), "accepted": accepted,
                "picked": sum(1 for e in events if e["event"] == "picked"),
                "accept_rate": round(100 * accepted / len(answered), 1) if answered else None}

    def driver_offer(self, driver_id: str) -> dict | None:
        """나에게 온 배차 제안 (없으면 None). 적재 요약은 '내 차량' 기준."""
        o = self._pending_offer_of(driver_id)
        if not o:
            return None
        v = self.order_view(o)
        v["packing"] = self._packing_summary(o, self.drivers[driver_id]["truck_type"])
        return v

    def driver_offer_history(self, driver_id: str, limit: int = 8) -> list[dict]:
        rows = []
        for o in self.orders.values():
            for e in o["match_log"]:
                if e["driver_id"] == driver_id and e["event"] in ("accepted", "declined", "expired", "assigned", "picked"):
                    rows.append({**e, "order_id": o["id"], "title": o["title"], "origin": o["origin"],
                                 "destination": o["destination"], "fare": o["price"] + o["incentive"]})
        rows.sort(key=lambda r: r["at"], reverse=True)
        return rows[:limit]

    def _check_offer(self, driver_id: str, order_id: str) -> dict:
        o = self._get_order(order_id)
        off = o["offer"]
        if o["status"] != "MATCHING" or not off or off["driver_id"] != driver_id:
            raise ApiError(409, "유효한 배차 제안이 아닙니다. 응답 시간이 지나 다음 기사에게 넘어갔을 수 있어요.")
        return o

    def accept_offer(self, driver_id: str, order_id: str) -> dict:
        o = self._check_offer(driver_id, order_id)
        if self._active_run_of(driver_id):
            raise ApiError(409, "진행 중인 운행을 먼저 완료해 주세요.")
        self._assign(o, driver_id, by="auto")
        return self.order_view(o)

    def decline_offer(self, driver_id: str, order_id: str) -> dict:
        o = self._check_offer(driver_id, order_id)
        now = datetime.now()
        o["declined"].append(driver_id)
        o["offer"] = None
        self._log(o, "declined", driver_id, now)
        self._run_matching(o, now)  # 바로 다음 순위에게
        return {"ok": True}

    def driver_recommendations(self, driver_id: str) -> list[dict]:
        """AI 추천 리스트: 내 차량에 전량 실리는 매칭 대기 화물을 매칭 점수(거리 + 적재율, 관제 설정 가중치) 순으로.
        반경 밖 화물도 포함(점수 낮게), 나에게 온 제안은 위 카드에 따로 보여 주므로 제외."""
        d = self.drivers[driver_id]
        if not self.driver_dispatchable(d):
            return []  # 가입 승인 전 · 자가용 번호판 · 블랙리스트
        here = self._driver_coord(d)
        rows = []
        for o in self.orders.values():
            if o["status"] != "MATCHING":
                continue
            off = o["offer"]
            if off and off["driver_id"] == driver_id:
                continue
            f = self._fit(o, d["truck_type"])
            if not f["fits"]:
                continue
            km = round(geo.route_km(here, geo.geocode(o["origin"])), 1)
            v = self.order_view(o)
            v["packing"] = self._packing_summary(o, d["truck_type"])
            v["rec"] = {
                "pickup_km": km, "util": f["util"], "volume_util": f["volume_util"], "weight_util": f["weight_util"],
                **self._pair_score(o, driver_id, km, f["util"]), "in_radius": km <= self.settings["radius_km"],
                "declined_by_me": driver_id in o["declined"],
                "offered_to_other": self.drivers[off["driver_id"]]["name"] if off else None,
                "offer_left": max(0, int((off["expires_at"] - datetime.now()).total_seconds())) if off else None,
            }
            rows.append(v)
        rows.sort(key=lambda v: (v["rec"]["offered_to_other"] is not None, -v["rec"]["score"], v["rec"]["pickup_km"]))
        for i, v in enumerate(rows, start=1):
            v["rec"]["rank"] = i
        return rows

    def accept_pick(self, driver_id: str, order_id: str) -> dict:
        """AI 추천 리스트에서 기사가 직접 골라 수락 → 배차 확정."""
        o = self._get_order(order_id)
        if o["status"] != "MATCHING":
            raise ApiError(409, "이미 배차된 화물입니다.")
        if not self.driver_dispatchable(self.drivers[driver_id]):
            raise ApiError(403, "배차를 받을 수 없는 상태입니다 (가입 승인 전 · 사업용 번호판 아님 · 이용 제한).")
        if self._active_run_of(driver_id):
            raise ApiError(409, "진행 중인 운행을 먼저 완료해 주세요.")
        off = o["offer"]
        if off and off["driver_id"] != driver_id:
            raise ApiError(409, "다른 기사가 이 화물의 배차 제안에 응답하는 중입니다. 잠시 후 다시 확인해 주세요.")
        d = self.drivers[driver_id]
        if not self._fit(o, d["truck_type"])["fits"]:
            raise ApiError(400, f"내 차량({d['truck_type']}) 기준 적재 시뮬레이션에서 전량 적재되지 않습니다.")
        now = datetime.now()
        mine = self._pending_offer_of(driver_id)
        if mine and mine["id"] != order_id:
            # 나에게 온 다른 제안은 거절로 처리하고 바로 다음 순위에게 넘긴다
            mine["declined"].append(driver_id)
            mine["offer"] = None
            self._log(mine, "declined", driver_id, now, note="AI 추천 리스트에서 다른 화물 선택")
            self._run_matching(mine, now)
        self._assign(o, driver_id, by="auto" if off else "pick")
        return self.order_view(o)

    def pending_offers(self) -> list[dict]:
        rows = [o for o in self.orders.values() if o["status"] == "MATCHING" and o["offer"]]
        rows.sort(key=lambda o: o["offer"]["expires_at"])
        return [self.order_view(o) for o in rows]

    def failed_matches(self) -> list[dict]:
        out = []
        for o in self.orders.values():
            if o["status"] != "MATCHING" or o["offer"] or not o["match_fail"]:
                continue
            v = self.order_view(o)
            est = v["fare_est"]
            gap = pricing.price_gap_ratio(v["fare"], est["recommended"])
            reasons = list(o["match_fail"])
            if gap > 0.03:
                reasons.append(f"AI 적정 운임 대비 약 {gap * 100:.0f}% 저렴")
            v.update(recommended=est["recommended"], reasons=reasons)
            out.append(v)
        out.sort(key=lambda v: v["wait_seconds"], reverse=True)
        return out

    def candidates(self, order_id: str) -> list[dict]:
        """관제 직접 지정 후보: 이 화물이 전량 실리고 지금 비어 있는 기사 (반경 · 거절 이력 무관, 가까운 순)."""
        o = self._get_order(order_id)
        rows = [r for r in self._evaluate(o) if r["fits"] and not r["busy"]]
        return sorted(rows, key=lambda r: r["pickup_km"])

    def _repost(self, o: dict, event: str, note: str = ""):
        now = datetime.now()
        if o["offer"]:
            self._log(o, "cancelled", o["offer"]["driver_id"], now)
            o["offer"] = None
        o["declined"], o["match_fail"], o["match_fail_at"], o["posted_at"] = [], None, None, now
        self._log(o, event, None, now, note)
        self._run_matching(o, now)

    def add_incentive(self, order_id: str, amount: int) -> dict:
        o = self._get_order(order_id)
        if o["status"] != "MATCHING":
            raise ApiError(409, "매칭 대기 중인 화물이 아닙니다.")
        o["incentive"] += amount
        self._repost(o, "reposted", f"보조금 +{amount:,}원 · 거절 이력 초기화")
        return self.order_view(o)

    def rematch(self, order_id: str) -> dict:
        o = self._get_order(order_id)
        if o["status"] != "MATCHING":
            raise ApiError(409, "매칭 대기 중인 화물이 아닙니다.")
        self._repost(o, "rematch", "거절 이력 초기화 후 다시 매칭")
        return self.order_view(o)

    def force_assign(self, order_id: str, driver_id: str) -> dict:
        o = self._get_order(order_id)
        if o["status"] != "MATCHING":
            raise ApiError(409, "매칭 대기 중인 화물이 아닙니다.")
        if driver_id not in {c["id"] for c in self.candidates(order_id)}:
            raise ApiError(400, "지정할 수 없는 기사입니다. (운행 중 · 다른 제안 대기 중이거나 이 화물이 다 실리지 않음)")
        if o["offer"]:
            self._log(o, "cancelled", o["offer"]["driver_id"], datetime.now())
            o["offer"] = None
        self._assign(o, driver_id, by="admin")
        return self.order_view(o)

    def matching_stats(self) -> dict:
        """매칭 분석: 모든 값은 화물의 매칭 기록에서 계산한다."""
        matched = [o for o in self.orders.values() if o["match"]]
        failed = [o for o in self.orders.values() if o["status"] == "MATCHING" and o["match_fail"]]
        events = [e for o in self.orders.values() for e in o["match_log"]]
        answered = [e for e in events if e["event"] in ("accepted", "declined", "expired")]
        secs = [(o["assigned_at"] - o["created_at"]).total_seconds() for o in matched if o["assigned_at"]]
        kms = [o["match"]["pickup_km"] for o in matched]
        vols = [o["match"]["volume_util"] for o in matched if o["match"]["volume_util"] is not None]
        wgts = [o["match"]["weight_util"] for o in matched if o["match"].get("weight_util") is not None]
        # 유효 적재율 = 부피 · 중량 중 큰 쪽 (무거운 화물은 부피보다 중량이 먼저 찬다 · 매칭 점수와 같은 기준)
        utils = [o["match"]["util"] for o in matched if o["match"].get("util") is not None]
        # 배차 소요 분해: 시스템 처리(등록 → 첫 제안) + 기사 응답(수락한 기사에게 간 제안 → 수락)
        sys_secs, resp_secs = [], []
        for o in matched:
            log = o["match_log"]
            first = next((e for e in log if e["event"] == "offered"), None)
            acc = next((e for e in reversed(log) if e["event"] == "accepted"), None)
            if first:
                sys_secs.append(max(0.0, (datetime.fromisoformat(first["at"]) - o["created_at"]).total_seconds()))
            if acc:
                off = next((e for e in reversed(log) if e["event"] == "offered"
                            and e["driver_id"] == acc["driver_id"] and e["at"] <= acc["at"]), None)
                if off:
                    resp_secs.append((datetime.fromisoformat(acc["at"])
                                      - datetime.fromisoformat(off["at"])).total_seconds())
        auto = [o for o in matched if o["match"]["by"] in ("auto", "pick")]  # 관제 직접 지정 제외

        bins = [(0, 10), (10, 20), (20, 30), (30, 50), (50, None)]
        dist = [{"range": f"{lo}~{hi}km" if hi else f"{lo}km 이상",
                 "count": sum(1 for k in kms if k >= lo and (hi is None or k < hi))} for lo, hi in bins]
        mix: dict[str, int] = {}
        for o in matched:
            t = self.drivers[o["match"]["driver_id"]]["truck_type"]
            mix[t] = mix.get(t, 0) + 1

        recent = sorted((o for o in matched if o["assigned_at"]), key=lambda o: o["assigned_at"], reverse=True)[:10]
        drivers = []
        for d in self.drivers.values():
            mine = [o for o in matched if o["match"]["driver_id"] == d["id"]]
            st = self._driver_offer_stats(d["id"])
            run = self._active_run_of(d["id"])
            rt = self.driver_rating(d["id"])
            drivers.append({
                "name": d["name"], "plate": d["plate"], "truck_type": d["truck_type"], "matched": len(mine),
                "rating": rt["avg"], "reviews": rt["count"], "blacklisted": bool(d.get("blacklisted")),
                "avg_pickup_km": round(mean(o["match"]["pickup_km"] for o in mine), 1) if mine else None,
                "offers": st["offers"], "accept_rate": st["accept_rate"],
                "state": "운행중" if run else ("제안 대기" if self._pending_offer_of(d["id"]) else "공차"),
            })
        drivers.sort(key=lambda r: (-r["matched"], r["avg_pickup_km"] or 999))
        return {
            "kpi": {
                "match_success_rate": round(100 * len(matched) / (len(matched) + len(failed)), 1) if matched or failed else None,
                "auto_rate": round(100 * len(auto) / len(matched), 1) if matched else None,
                "avg_match_seconds": int(mean(secs)) if secs else None,
                "avg_offer_seconds": round(mean(sys_secs), 1) if sys_secs else None,      # 시스템 처리 (기록)
                # 시스템 매칭 속도 (실측): 제안을 보낸 배정 라운드의 소요 시간 평균 — 차량별 적재 재계산 · 거리 · AI 예측 · 헝가리안
                #   (제안을 보낸 라운드가 아직 없으면 후보를 평가한 라운드 전체 평균)
                "system_match_seconds": (lambda rs: round(mean(e["latency_ms"] for e in rs) / 1000, 2) if rs else None)(
                    [e for e in self.metrics_log if e["assigned"]] or [e for e in self.metrics_log if e["orders"]]),
                "avg_response_seconds": int(mean(resp_secs)) if resp_secs else None,     # 기사 응답
                "avg_pickup_km": round(mean(kms), 1) if kms else None,
                "avg_volume_util": round(mean(vols), 3) if vols else None,
                "avg_weight_util": round(mean(wgts), 3) if wgts else None,
                "avg_util": round(mean(utils), 3) if utils else None,
                "accept_rate": round(100 * sum(1 for e in answered if e["event"] == "accepted") / len(answered), 1)
                if answered else None,
            },
            "settings": self.settings_view(),
            "metrics": self.matching_metrics(),
            "engine": {  # 3단계 매칭 엔진 상태 (관제 '매칭 분석' 화면)
                "pipeline": self.settings_view()["pipeline"],
                "spatial": "PostGIS ST_DWithin" if getattr(self, "db", None) else "직선거리 (DB 미사용)",
                "ml": ml_ranker.info(), "ml_weight": ML_WEIGHT, "ml_threshold": ML_THRESHOLD,
                "last_round": getattr(self, "last_round", None),  # 최근 헝가리안 배정 vs 탐욕 방식 비교
            },
            "pickup_dist": dist,
            "truck_mix": [{"truck_type": k, "count": v} for k, v in sorted(mix.items(), key=lambda kv: -kv[1])],
            "recent": [{
                "id": o["id"], "title": o["title"], "driver": self.drivers[o["match"]["driver_id"]]["name"],
                "truck_type": self.drivers[o["match"]["driver_id"]]["truck_type"],
                "pickup_km": o["match"]["pickup_km"], "volume_util": o["match"]["volume_util"],
                "util": o["match"].get("util"), "score": o["match"]["score"], "offers": o["match"]["offers"],
                "by": MATCH_BY_LABEL[o["match"]["by"]],
                "match_seconds": int((o["assigned_at"] - o["created_at"]).total_seconds()),
                "assigned_at": _iso(o["assigned_at"]),
            } for o in recent],
            "drivers": drivers,
        }
