"""
Phase 5: 규칙 기반 AI 관제 보조 에이전트 (외부 LLM · API 키 없음)

관제 데이터를 정해진 규칙으로 점검해 '상황 → 원인 → 권장 조치 → 안내 문구 초안'을 만든다.
LLM 처럼 자유 대화를 하지는 않지만, 항상 같은 기준으로 판단하고 근거(수치)를 함께 보여 준다.

점검 규칙
  1. 매칭 실패      : 실패 원인 · AI 적정 운임 대비 운임 차이 · 가장 가까운 공차 거리 → 보조금 금액 / 반경 확대 제안
  2. 운행 이상      : 온도 이탈 · 장시간 정체 · 경로 이탈 → 기사 확인 요청 · 화주 안내 문구
  3. 도착 지연 위험  : 지연 30분 이상 → 화주 ETA 재안내 문구
  4. 응답 임박 제안  : 수락 제한시간 15초 이하 → 다음 순위 대비 안내
  5. 정산 지연      : 청구 후 3일 넘게 미승인 → 화주 승인 요청
  6. 가입 심사 대기  : 승인 대기 차주 → 서류 확인 요청
  7. 평판 · 제재     : 블랙리스트 · 평점 주의 기사
  8. 수급 서지       : D/S 1.5 초과 권역 → 운임 할증 · 공차 유도 안내
결과: severity(HIGH/MEDIUM/LOW) 순으로 정렬한 인사이트 목록 + 한 줄 브리핑

그 밖에
  tuning_guide()    : 거절률 · 임계값 차단 · 평점으로 LightGBM 임계값 · 가중치 조절 가이드
  incident_report() : 이상 감지 한 건 → 액션 아이템 형태 인시던트 리포트
  handle_command()  : 관제 자연어 명령을 키워드 규칙으로 처리 (Gemini 키가 없을 때 대신 사용)
"""

from __future__ import annotations

from datetime import datetime

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
AGENT_NAME = "규칙 기반 AI 관제 보조"


def _round_k(v: float, unit: int = 10_000) -> int:
    return max(unit, int(round(v / unit)) * unit)


def _insight(severity, category, title, detail, evidence=None, action=None, message=None, order_id=None):
    return {"severity": severity, "category": category, "title": title, "detail": detail,
            "evidence": evidence or [], "action": action, "message": message, "order_id": order_id}


# ------------------------------------------------------------------ 규칙
def rule_failed_matches(store) -> list[dict]:
    out = []
    radius = store.settings["radius_km"]
    for v in store.failed_matches():
        est, fare = v["recommended"], v["fare"]
        gap = (est - fare) / est if est else 0.0
        reasons = v["reasons"]
        nearest = None
        for r in reasons:
            if "가장 가까운 공차" in r:
                try:
                    nearest = float(r.split("가장 가까운 공차")[1].strip(" )km"))
                except ValueError:
                    pass
        if gap > 0.03:
            amount = min(300_000, _round_k(est - fare))
            action = {"type": "incentive", "label": f"보조금 +{amount:,}원 재공고", "amount": amount,
                      "endpoint": f"/admin/failed-matches/{v['id']}/incentive"}
            why = f"운임이 AI 적정 운임보다 {gap * 100:.0f}% 낮아 기사 선호도가 떨어집니다."
        elif nearest and nearest > radius:
            action = {"type": "radius", "label": f"탐색 반경 {int(min(200, nearest + 10))}km 로 확대 검토",
                      "suggest_radius": int(min(200, nearest + 10))}
            why = f"반경 {radius}km 안에 공차가 없고 가장 가까운 공차가 {nearest:.0f}km 에 있습니다."
        else:
            action = {"type": "rematch", "label": "거절 이력 초기화 후 재매칭",
                      "endpoint": f"/admin/failed-matches/{v['id']}/rematch"}
            why = "적재 가능한 차량이 운행 중이거나 거절했습니다. 공차가 생기면 다시 배정됩니다."
        wait = (v["wait_seconds"] or 0) // 60
        out.append(_insight(
            "HIGH" if wait >= 30 or v["urgent"] else "MEDIUM", "매칭 실패",
            f"{v['id']} {v['title']} — 배차 실패 {wait}분째", why,
            evidence=reasons + [f"제시 운임 {fare:,}원 · AI 적정 {est:,}원"], action=action, order_id=v["id"],
            message=(f"[배차의 민족] {v['shipper']} 담당자님, {v['origin']} → {v['destination']} 화물({v['id']})의 "
                     f"배차가 지연되고 있습니다. 현재 조건에 맞는 차량을 찾고 있으며, 운임 조정 시 배차가 빨라질 수 있습니다.")))
    return out


def rule_anomalies(store) -> list[dict]:
    out = []
    for a in store.list_anomalies("ALL", "UNSOLVED"):
        if a["type"] == "COLD":
            why = f"설정 {a['temp_set']}℃ 대비 현재 {a['temp_now']}℃ — 화물 품질 위험."
            msg = f"[관제] {a['driver']} 기사님, 냉동기 설정 온도({a['temp_set']}℃)를 확인하고 문 닫힘 상태를 점검해 주세요."
            sev = "HIGH"
        elif a["type"] == "STAGNATION":
            why = "장시간 정체 — 사고 · 고장 · 휴게 여부 확인 필요."
            msg = f"[관제] {a['driver']} 기사님, 현재 차량이 오래 멈춰 있습니다. 상황을 알려 주세요. 필요하면 우회 경로를 안내드리겠습니다."
            sev = "HIGH" if a["severity"] == "HIGH" else "MEDIUM"
        elif a["type"] == "SUSPICIOUS_REOUTSOURCING":
            why = "배정 기사가 아닌 단말이 위치를 보고 — 불법 다단계 재위탁 의심."
            msg = f"[관제] {a['driver']} 기사님, 배정 차량 단말이 아닌 곳에서 위치가 보고되었습니다. 직접 운행 중인지 확인해 주세요."
            sev = "HIGH"
        elif a["type"] == "DELAY":
            why = "계획 ETA 대비 30분 넘게 늦어짐 — 화주 안내 필요."
            msg = f"[관제] {a['driver']} 기사님, 도착이 늦어지고 있습니다. 예상 도착 시각을 알려 주세요."
            sev = "MEDIUM"
        else:
            why = "계획 경로를 벗어남 (연속 3회 확인 · 디바운싱 통과) — 우회 · 오배송 여부 확인 필요."
            msg = f"[관제] {a['driver']} 기사님, 계획 경로에서 벗어났습니다. 우회 중이시면 사유를 알려 주세요."
            sev = "MEDIUM"
        out.append(_insight(sev, "운행 이상", f"{a['type_label']} · {a['plate']} ({a['driver']})", why,
                            evidence=[a["text"], a.get("detail") or "", f"위치 {a['location']}"],
                            action={"type": "resolve", "label": "확인 후 조치 완료",
                                    "endpoint": f"/admin/anomalies/{a['id']}/resolve"},
                            message=msg, order_id=a["order_id"]))
    return out


def rule_delays(store) -> list[dict]:
    out = []
    for o in store.orders.values():
        if o["status"] != "IN_TRANSIT" or o["delay_minutes"] < 30:
            continue
        v = store.order_view(o)
        out.append(_insight("MEDIUM", "도착 지연", f"{o['id']} {o['title']} — 약 {o['delay_minutes']}분 지연 예상",
                            "예상 도착 시각을 화주에게 다시 안내하는 것을 권장합니다.",
                            evidence=[f"진행률 {v['progress']:.0f}% · 속도 {v['speed']}km/h", f"도착 예정 {v['eta_clock']}"],
                            message=(f"[배차의 민족] {v['shipper']} 담당자님, {o['id']} 화물이 교통 상황으로 약 "
                                     f"{o['delay_minutes']}분 늦어져 {v['eta_clock']} 도착 예정입니다. 양해 부탁드립니다."),
                            order_id=o["id"]))
    return out


def rule_offers(store) -> list[dict]:
    out = []
    for v in store.pending_offers():
        m = v["matching"]
        if m and m["expires_in"] <= 15:
            out.append(_insight("LOW", "응답 임박", f"{v['id']} — {m['driver']['name']} 기사 응답 {m['expires_in']}초 남음",
                                "응답이 없으면 헝가리안 배정이 다음 후보에게 다시 제안합니다.",
                                evidence=[f"점수 {m['score'] * 100:.0f} · 후보 {m['candidates']}대"], order_id=v["id"]))
    return out


def rule_settlement(store) -> list[dict]:
    out = []
    now = datetime.now()
    for o in store.orders.values():
        if o["billing"] != "REQUESTED" or not o["requested_at"]:
            continue
        days = (now - o["requested_at"]).total_seconds() / 86400
        if days >= 3:
            total = o["price"] + o["incentive"] + o["extra_fee"] + o["toll_fee"]
            shipper = store.shippers.get(o["shipper_id"], {}).get("name", "-")
            out.append(_insight("LOW", "정산 지연", f"{o['id']} 청구서 {days:.0f}일째 미승인 ({total:,}원)",
                                "차주 현금 흐름에 영향 — 화주에게 승인 요청을 권장합니다.",
                                message=f"[배차의 민족] {shipper} 담당자님, {o['id']} 운송비 청구서({total:,}원)가 승인 대기 중입니다.",
                                order_id=o["id"]))
    return out


def rule_compliance(store) -> list[dict]:
    pending = [d for d in store.compliance_drivers() if d["status"] == "PENDING"]
    if not pending:
        return []
    return [_insight("MEDIUM", "가입 심사", f"차주 가입 승인 대기 {len(pending)}명",
                     "승인 전에는 배차를 받을 수 없습니다. 제출 서류를 확인해 주세요.",
                     evidence=[f"{d['name']} · {d['truck_type']} · {d['plate']}" for d in pending[:5]],
                     action={"type": "link", "label": "컴플라이언스에서 심사", "page": "compliance"})]


def rule_reputation(store) -> list[dict]:
    out = []
    for r in store.reputation_board():
        if r["blacklisted"]:
            b = r["blacklist"] or {}
            out.append(_insight("LOW", "평판 · 제재", f"{r['name']} ({r['plate']}) 이용 제한 중",
                                f"{b.get('reason', '')} — 매칭 후보에서 제외되어 있습니다.",
                                evidence=[f"평점 {r['avg']} · 리뷰 {r['count']}건", ", ".join(r["top_tags"])]))
        elif r["at_risk"]:
            out.append(_insight("LOW", "평판 · 제재", f"{r['name']} 평점 주의 ({r['avg']}점 · {r['count']}건)",
                                "리뷰가 한 건 더 나쁘면 자동 이용 제한 기준에 걸립니다. 운행 품질 안내를 권장합니다.",
                                evidence=[", ".join(r["top_tags"])]))
    return out


def rule_market(store) -> list[dict]:
    out = []
    for m in store.market_board():
        if not m["surge_on"]:
            continue
        out.append(_insight("LOW", "수급 서지", f"{m['region']} 수요 초과 D/S {m['ds_ratio']} → 운임 +{m['surge']:.0%}",
                            "콜이 빈 차량보다 1.5배 넘게 많습니다. 인접 권역 공차에 이동 인센티브를 안내하는 것을 권장합니다.",
                            evidence=[f"수요 {m['demand']} (실제 {m['real_demand']}) · 공급 {m['supply']} (실제 {m['real_supply']})",
                                      f"날씨 {m['weather']['label']}" + (" · 러시아워" if m["rush_hour"] else "")]))
    return out


RULES = [rule_failed_matches, rule_anomalies, rule_delays, rule_offers, rule_settlement, rule_compliance,
         rule_reputation, rule_market]


# ------------------------------------------------------------------ 임계값 · 가중치 조절 가이드
def tuning_guide(store) -> dict:
    """거절률 · 임계값 차단 · 평점 피드백으로 매칭 설정 조절 방향을 제안 (자동 적용하지 않음)."""
    m = store.matching_metrics()
    s = store.settings
    rej, filtered = m["rejection_rate_pct"], m["threshold_filtered"]
    failed = len(store.failed_matches())
    th = m["ml_threshold"]
    tips = []
    if rej is not None and rej > 30:
        tips.append({"target": "lgb_score 임계값", "now": th, "suggest": round(min(0.9, th + 0.05), 2),
                     "why": f"기사 거절률 {rej}% > 30% — 수락 가능성이 낮은 제안을 더 걸러야 합니다."})
    if filtered and failed:
        tips.append({"target": "lgb_score 임계값", "now": th, "suggest": round(max(0.6, th - 0.05), 2),
                     "why": f"임계값 미달로 {filtered}쌍이 걸러졌고 매칭 실패 {failed}건 — 너무 보수적일 수 있습니다."})
    surge = [x["region"] for x in store.market_board() if x["surge_on"]]
    if surge:
        tips.append({"target": "탐색 반경", "now": s["radius_km"], "suggest": min(200, s["radius_km"] + 20),
                     "why": f"수요 초과 권역({', '.join(surge[:4])})은 반경을 넓혀 인접 공차를 끌어오는 것이 유리합니다."})
    low = [r for r in store.reputation_board() if r["avg"] is not None and r["avg"] < 3.5 and not r["blacklisted"]]
    if low:
        tips.append({"target": "평점 피처", "now": "LightGBM rating", "suggest": "유지",
                     "why": f"평점 3.5 미만 기사 {len(low)}명 — 평점이 수락 예측에 반영되고 있어 별도 조치 불필요."})
    if not tips:
        tips.append({"target": "현재 설정", "now": f"반경 {s['radius_km']}km · 임계값 {th}", "suggest": "유지",
                     "why": "거절률 · 실패 · 수급 모두 기준 안입니다."})
    return {"metrics": {k: m[k] for k in ("rejection_rate_pct", "threshold_filtered", "dispatch_latency_p95_sec",
                                           "empty_deadhead_km_reduction_pct")}, "failed_matches": failed, "tips": tips}


# ------------------------------------------------------------------ 인시던트 리포트
def incident_report(store, anomaly_id: str) -> dict:
    a = next((x for x in store.list_anomalies() if x["id"] == anomaly_id), None)
    if not a:
        return {"ok": False, "text": "해당 이상 감지 건을 찾을 수 없습니다."}
    o = store.orders.get(a["order_id"]) if a["order_id"] else None
    items = {
        "COLD": ["기사에게 냉동기 · 문 닫힘 즉시 확인 요청", "화주 품질 담당에게 온도 이탈 시각 · 폭 공유", "하차 시 온도 기록지 확인"],
        "STAGNATION": ["기사 통화로 사고 · 고장 여부 확인", "30분 이상이면 대체 차량 수배 검토", "화주에게 ETA 재안내"],
        "DEVIATION": ["기사에게 우회 사유 확인 (통제 · 휴게 · 오배송)", "사유 불명확 시 경로 복귀 안내",
                      "반복되면 리뷰 · 평판 모니터링 대상 등록"],
        "DELAY": ["화주에게 새 도착 예정 시각 안내", "하차지 도크 대기 조정 요청", "지연 사유 기록 (정산 분쟁 대비)"],
        "SUSPICIOUS_REOUTSOURCING": ["배정 기사와 즉시 통화 · 실제 운전자 확인", "재위탁 확인 시 운행 중지 · 이용 제한 검토",
                                     "감사 로그 해시 체인으로 보고 이력 보존"],
    }.get(a["type"], ["상황 확인"])
    lines = [f"[인시던트 리포트] {a['id']} · {a['type_label']} · 심각도 {a['severity']}",
             f"- 발생: {a['at']} · {a['location']}", f"- 차량: {a['plate']} ({a['driver']}) · 연락처 {a['phone']}",
             f"- 내용: {a['text']} / {a.get('detail') or ''}"]
    if o:
        lines.append(f"- 화물: {o['id']} {o['title']} · {o['origin']} → {o['destination']} · 진행률 {o['progress']:.0f}%")
    lines.append("액션 아이템")
    lines += [f"  {i}. {t}" for i, t in enumerate(items, start=1)]
    return {"ok": True, "engine": "rule-based", "anomaly_id": anomaly_id, "text": "\n".join(lines),
            "action_items": items}


# ------------------------------------------------------------------ 자연어 명령 (규칙 · Gemini 대체)
def handle_command(store, text: str) -> dict:
    """키워드 규칙으로 관제 명령을 처리한다. 예) '매칭 실패 보여줘', '지연 차량', '반경 70km로', '수급', '임계값'."""
    import re
    t = text.strip()
    if not t:
        return {"engine": "rule-based", "answer": "명령을 입력해 주세요.", "data": None}
    m = re.search(r"반경\s*(\d{2,3})\s*km", t)
    if m and any(k in t for k in ("바꿔", "변경", "로", "설정")):
        r = int(m.group(1))
        if not 10 <= r <= 200:
            return {"engine": "rule-based", "answer": "반경은 10~200km 사이로 지정해 주세요.", "data": None}
        store.update_settings({**store.settings, "radius_km": r}, by="AI 관제 (규칙)")
        return {"engine": "rule-based", "answer": f"탐색 반경을 {r}km 로 바꾸고 매칭 실패 화물부터 다시 매칭했습니다.",
                "data": store.settings_view(), "applied": True}
    if "실패" in t:
        rows = store.failed_matches()
        ans = f"매칭 실패 {len(rows)}건: " + "; ".join(f"{v['id']} {v['title']} ({v['reasons'][0]})" for v in rows[:5])
        return {"engine": "rule-based", "answer": ans if rows else "매칭 실패 화물이 없습니다.", "data": rows[:5]}
    if "지연" in t:
        rows = [store.order_view(o) for o in store.orders.values() if o["status"] == "IN_TRANSIT" and o["delay_minutes"] >= 30]
        ans = "; ".join(f"{v['id']} {v['driver']['name'] if v['driver'] else '-'} +{v['delay_minutes']}분" for v in rows)
        return {"engine": "rule-based", "answer": f"30분 이상 지연 {len(rows)}건: {ans}" if rows else "30분 이상 지연된 운행이 없습니다.",
                "data": rows}
    if "이탈" in t or "재위탁" in t or "이상" in t:
        rows = store.list_anomalies("ALL", "UNSOLVED")
        ans = "; ".join(f"{a['type_label']} {a['plate']} ({a['text']})" for a in rows[:6])
        return {"engine": "rule-based", "answer": f"미조치 이상 {len(rows)}건: {ans}" if rows else "미조치 이상 감지가 없습니다.",
                "data": rows[:6]}
    if "수급" in t or "서지" in t or "날씨" in t:
        rows = store.market_board()
        hot = [f"{x['region']} D/S {x['ds_ratio']}(+{x['surge']:.0%}, {x['weather']['label']})" for x in rows if x["surge_on"]]
        return {"engine": "rule-based", "answer": ("서지 발동 권역: " + ", ".join(hot)) if hot else "서지 발동 권역이 없습니다.",
                "data": rows}
    if "임계" in t or "가중치" in t or "튜닝" in t:
        g = tuning_guide(store)
        return {"engine": "rule-based", "answer": " / ".join(f"{x['target']}: {x['now']} → {x['suggest']} ({x['why']})"
                                                            for x in g["tips"]), "data": g}
    if "블랙" in t or "평점" in t or "리뷰" in t:
        rows = store.reputation_board()[:6]
        ans = "; ".join(f"{r['name']} {r['avg'] or '-'}점({r['count']}건){' · 제한' if r['blacklisted'] else ''}" for r in rows)
        return {"engine": "rule-based", "answer": ans, "data": rows}
    b = briefing(store)
    return {"engine": "rule-based", "answer": b["summary"], "data": b["counts"],
            "hint": "예) '매칭 실패 보여줘' · '지연 차량' · '경로 이탈' · '수급 현황' · '임계값 가이드' · '반경 70km로 바꿔'"}


def briefing(store) -> dict:
    insights = []
    for rule in RULES:
        insights += rule(store)
    insights.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    counts = {s: sum(1 for i in insights if i["severity"] == s) for s in SEVERITY_ORDER}
    cats: dict[str, int] = {}
    for i in insights:
        cats[i["category"]] = cats.get(i["category"], 0) + 1
    if insights:
        top = insights[0]
        summary = (f"긴급 {counts['HIGH']}건 · 주의 {counts['MEDIUM']}건 · 참고 {counts['LOW']}건. "
                   f"가장 먼저: {top['title']} → {top['action']['label'] if top['action'] else '확인'}")
    else:
        summary = "지금 조치가 필요한 항목이 없습니다. 모든 운행 · 매칭이 정상입니다."
    return {"agent": AGENT_NAME, "engine": "rule-based (LLM · 외부 API 미사용)",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": summary, "counts": counts, "categories": cats, "insights": insights}
