"""
적재 결과 독립 검증기.

엔진 내부 로직과 별개로, '결과 좌표만 보고' 모든 제약을 다시 검사한다.
→ 우리 EP 엔진의 결과가 정말 제약을 지키는지 확인하고,
→ py3dbp 같은 외부 라이브러리 결과도 같은 잣대로 비교할 수 있다.

입력 rows: Placement.to_dict() 형식 (x,y,z,length,width,height,weight_kg,orig,upright,stackable,max_top_load,drop)
"""

from ..validation_engine import axle_balance, safety_check
from .trucks import Container

EPS = 1e-6


def _overlap_1d(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def validate(container: Container, rows: list[dict], *, min_support: float, lifo: bool,
             cold_chain: bool = False, safety_margin: float = 0.10) -> list[dict]:
    c = container
    n = len(rows)
    box = [(r["x"], r["x"] + r["length"], r["y"], r["y"] + r["width"], r["z"], r["z"] + r["height"]) for r in rows]
    ids = [r["item_id"] for r in rows]
    checks = []

    def add(key, name, rule, bad, detail_ok, detail_bad, warn=False):
        status = "pass" if not bad else ("warn" if warn else "fail")
        checks.append({"key": key, "name": name, "rule": rule, "status": status,
                       "detail": detail_ok if not bad else detail_bad, "violations": sorted(set(bad))[:50]})

    # C0 차량-화물 호환
    add("compat", "차량-화물 호환", "냉장 화물은 냉장 차량에만", ["-"] if cold_chain and not c.refrigerated else [],
        "냉장 조건 충족" if cold_chain else "상온 화물", f"{c.name}은(는) 냉장 차량이 아닙니다")

    # C1 경계
    bad = [ids[i] for i, (x0, x1, y0, y1, z0, z1) in enumerate(box)
           if x0 < 0 or y0 < 0 or z0 < 0 or x1 > c.length or y1 > c.width or z1 > c.height]
    add("bounds", "적재함 경계", "모든 상자가 적재함 안쪽", bad, "모두 적재함 내부", f"{len(bad)}개 상자가 적재함 밖으로 돌출")

    # C2 비중첩
    bad = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = box[i], box[j]
            if (_overlap_1d(a[0], a[1], b[0], b[1]) > EPS and _overlap_1d(a[2], a[3], b[2], b[3]) > EPS
                    and _overlap_1d(a[4], a[5], b[4], b[5]) > EPS):
                bad += [ids[i], ids[j]]
    add("overlap", "상자 간 비중첩", "부피가 겹치는 상자 없음", bad, "겹침 없음", f"{len(set(bad))}개 상자가 서로 겹침")

    # C3 방향
    bad = [r["item_id"] for r in rows if r["upright"] and r["height"] != r["orig"][2]]
    add("orientation", "방향 (세움 고정)", "세움 고정 품목은 높이축 유지", bad,
        "세움 고정 품목 모두 정방향", f"{len(bad)}개 품목이 눕혀지거나 뒤집힘")

    # C4 중량
    total = sum(r["weight_kg"] for r in rows)
    add("weight", "최대 적재중량", f"총 중량 ≤ {c.max_weight:,.0f}kg", ["-"] if total > c.max_weight + EPS else [],
        f"{total:,.0f}kg / {c.max_weight:,.0f}kg", f"{total:,.0f}kg — {total - c.max_weight:,.0f}kg 초과")

    # C5 지지면 + 하중 분담 계산
    supporters: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    floating, min_ratio = [], 1.0
    for i in range(n):
        x0, x1, y0, y1, z0, _ = box[i]
        if z0 <= EPS:
            continue
        areas = []
        for j in range(n):
            if i != j and abs(box[j][5] - z0) < EPS:
                ox, oy = _overlap_1d(x0, x1, box[j][0], box[j][1]), _overlap_1d(y0, y1, box[j][2], box[j][3])
                if ox > EPS and oy > EPS:
                    areas.append((j, ox * oy))
        tot = sum(a for _, a in areas)
        ratio = tot / ((x1 - x0) * (y1 - y0))
        min_ratio = min(min_ratio, ratio)
        if ratio + EPS < min_support:
            floating.append(ids[i])
        if tot > 0:
            supporters[i] = [(j, a / tot) for j, a in areas]
    add("support", "지지면 (안정성)", f"바닥 면적의 {min_support:.0%} 이상이 아래 상자/바닥에 닿음", floating,
        f"최소 지지 비율 {min_ratio:.0%}", f"{len(floating)}개 상자가 지지 부족 (공중 부양 위험)")

    # C6 적층 불가 / 상부 허용하중 — 위에서부터 아래로 하중 누적
    load = [0.0] * n
    for i in sorted(range(n), key=lambda k: -box[k][4]):
        for j, share in supporters[i]:
            load[j] += (rows[i]["weight_kg"] + load[i]) * share
    bad, worst = [], 0.0
    for j, r in enumerate(rows):
        cap = 0.0 if not r["stackable"] else r["max_top_load"]
        if cap is None:
            continue
        if load[j] > cap + EPS:
            bad.append(ids[j])
        if cap > 0:
            worst = max(worst, load[j] / cap)
    add("stacking", "적층 · 상부 허용하중", "파손주의 품목 위 적재 금지, 상부 하중 ≤ 허용하중", bad,
        f"최대 하중 사용률 {worst:.0%}" if worst else "하중 제한 품목 위 적재 없음",
        f"{len(bad)}개 상자가 허용하중 초과/파손주의 위 적재")

    # C7 하차 순서
    bad = []
    if lifo:
        for i in range(n):
            for j in range(n):
                if rows[i]["drop"] >= rows[j]["drop"]:
                    continue  # i 가 먼저 내려야 하는 경우만 검사
                a, b = box[i], box[j]  # b(나중에 내림)가 a(먼저 내림)를 막는가
                xy = _overlap_1d(a[0], a[1], b[0], b[1]) > EPS and _overlap_1d(a[2], a[3], b[2], b[3]) > EPS
                yz = _overlap_1d(a[2], a[3], b[2], b[3]) > EPS and _overlap_1d(a[4], a[5], b[4], b[5]) > EPS
                if (xy and b[4] >= a[5] - EPS) or (not c.side_access and yz and b[0] >= a[1] - EPS):
                    bad += [ids[i], ids[j]]
    drops = sorted({r["drop"] for r in rows})
    access = "측면 하역 가능 차량: 위 적재만 검사" if c.side_access else "후문 하역: 위·도어 방향 가로막힘 검사"
    add("lifo", "하차 순서 (LIFO)", access, bad,
        f"하차지 {len(drops)}곳 · 막힘 없음" if lifo else "검사 꺼짐", f"{len(set(bad))}개 상자가 하차 순서상 가로막힘")

    # C8 보수적 안전율 (engines/validation_engine.py) — 포장 · 측정 오차 감안, 용량의 90% 초과 시 차단
    vol_util = sum(r["length"] * r["width"] * r["height"] for r in rows) / c.volume if c.volume else 0
    wt_util = total / c.max_weight if c.max_weight else 0
    sc = safety_check(vol_util, wt_util, safety_margin)
    checks.append({"key": "safety", "name": f"보수적 안전율 ({safety_margin:.0%})",
                   "rule": f"부피 · 중량 사용률 {sc['warn_at']:.0%} 초과 경고 · {sc['limit']:.0%} 초과 차단",
                   "status": sc["status"], "detail": sc["detail"], "violations": [] if sc["status"] == "pass" else ["-"]})

    # C9 축중량 균형 (산안법 제173조 편하중 방지) — 무게중심 · 앞축/뒤축 하중
    ab = axle_balance(rows, c.length, c.width, c.max_weight)
    checks.append({"key": "cog", "name": "축중량 균형 (산안법 제173조)",
                   "rule": "좌우 편차 ≤ 10%(15% 초과 차단), 전후 무게중심 20~65%(15~75% 밖 차단)",
                   "status": ab["status"],
                   "detail": ab["detail"] + (" — 무게 배분 조정 필요" if ab["status"] != "pass" else ""),
                   "violations": [] if ab["status"] == "pass" else ["-"], "axle": ab})
    return checks
