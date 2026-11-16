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
결과: severity(HIGH/MEDIUM/LOW) 순으로 정렬한 인사이트 목록 + 한 줄 브리핑
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
        else:
            why = "계획 경로를 벗어남 — 우회 · 오배송 여부 확인 필요."
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


RULES = [rule_failed_matches, rule_anomalies, rule_delays, rule_offers, rule_settlement, rule_compliance]


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
