"""
빈패킹 서비스: 입력 품목 → (차량 자동 추천) → 규칙 + EP 엔진 → 규칙 검증.
라우터와 화물 등록 로직은 이 함수(plan)만 호출한다.
"""

from dataclasses import replace

from .engine import EPPacker, Options, summarize
from .models import ItemSpec, expand
from .rules import describe
from .trucks import TRUCKS, compatible_trucks
from .validate import validate

MAX_BOXES = 300


class PackingError(ValueError):
    pass


def _safety_margin() -> float:
    try:
        from ...config import settings
        return settings.safety_margin
    except Exception:  # noqa: BLE001 — 엔진 단독 실행
        return 0.10


def _run(container, boxes, options: Options, cold_chain: bool) -> dict:
    """R10 좌우 균형 배치는 상자마다 그 순간 최선을 고르는 방식이라 드물게 전체 결과가 더 나빠질 수 있다.
    → 균형 배치 결과가 '전량 적재 + 위반 없음'이 아니면 균형 없이도 한 번 더 돌려 더 나은 쪽을 쓴다."""
    r = _run_once(container, boxes, options, cold_chain)
    if options.balance and not _ok(r):
        r2 = _run_once(container, boxes, replace(options, balance=False), cold_chain)
        if _score(r2) > _score(r):
            r = r2
    return r


def _score(r: dict) -> tuple:
    fails = sum(1 for c in r["constraints"] if c["status"] == "fail")
    lateral = r["metrics"]["cog"]["y_offset_ratio"] or 0
    return (_ok(r), r["metrics"]["placed"], -fails, -lateral)


def _run_once(container, boxes, options: Options, cold_chain: bool) -> dict:
    res = EPPacker(container, options).pack(boxes)
    rows = [p.to_dict() for p in res["placements"]]
    total_w = sum(b.weight for b in boxes)
    return {
        "truck": container.as_dict(),
        "placements": rows,
        "unplaced": res["unplaced"],
        "steps": res["steps"],
        "metrics": {**summarize(container, rows, len(boxes), total_w), "elapsed_ms": res["elapsed_ms"]},
        "constraints": validate(container, rows, min_support=options.min_support, lifo=options.lifo,
                                cold_chain=cold_chain, safety_margin=_safety_margin()),
        "balance_used": options.balance,
    }


def _ok(r: dict) -> bool:
    """전량 적재 + 필수 제약(안전율 · 축중량 포함) 위반 없음."""
    return r["metrics"]["unplaced"] == 0 and not any(c["status"] == "fail" for c in r["constraints"])


def plan(items: list[ItemSpec], *, truck_type: str | None, cold_chain: bool, options: Options) -> dict:
    options = replace(options, safety_margin=_safety_margin())  # R9: 엔진도 안전율 한도까지만 싣는다
    boxes = expand(items)
    if not boxes:
        raise PackingError("적재할 품목을 1개 이상 입력해 주세요.")
    if len(boxes) > MAX_BOXES:
        raise PackingError(f"한 번에 시뮬레이션할 수 있는 상자는 최대 {MAX_BOXES}개입니다. (현재 {len(boxes)}개)")
    total_w = sum(b.weight for b in boxes)
    total_v = sum(b.volume for b in boxes)

    candidates = []
    if truck_type:
        if truck_type not in TRUCKS:
            raise PackingError("지원하지 않는 차종입니다.")
        result = _run(TRUCKS[truck_type], boxes, options, cold_chain)
        chosen = result
    else:
        chosen, best = None, None
        for t in compatible_trucks(cold_chain):
            row = {"truck_type": t.name, "evaluated": False, "placed": 0, "total": len(boxes),
                   "volume_util": None, "weight_util": None, "fits": False}
            # 사전 검사: 안전율을 적용한 한도를 이미 넘으면 시뮬레이션 생략
            limit = 1 - _safety_margin()
            if total_w > t.max_weight * limit:
                row["note"] = "총중량 초과 (안전율 적용)"
            elif total_v > t.volume * limit:
                row["note"] = "총부피 초과 (안전율 적용)"
            elif chosen is not None:
                row["note"] = "더 작은 차량으로 충분"
            else:
                r = _run(t, boxes, options, cold_chain)
                m = r["metrics"]
                failed = [c["name"] for c in r["constraints"] if c["status"] == "fail"]
                row.update(evaluated=True, placed=m["placed"], volume_util=m["volume_util"],
                           weight_util=m["weight_util"], fits=_ok(r),
                           note=("전량 적재" if _ok(r) else f"{m['unplaced']}개 미적재" if m["unplaced"]
                                 else "제약 위반: " + ", ".join(failed)))
                if _ok(r):
                    chosen = r
                if best is None or m["placed"] > best["metrics"]["placed"]:
                    best = r
            candidates.append(row)
        chosen = chosen or best
        if chosen is None:
            raise PackingError("냉장/중량/부피 조건을 만족하는 차량이 없습니다. 품목을 나누어 등록해 주세요.")

    from ..validation_engine import required_tonnage
    out = {**chosen, "auto": truck_type is None, "candidates": candidates,
           "options": vars(options), "cold_chain": cold_chain, "safety_margin": _safety_margin(),
           "required_tonnage": required_tonnage(total_w, total_v, list(TRUCKS.values()), _safety_margin(),
                                                cold_chain),
           "rules": describe()}
    return out


def team_format(order_id: str, result: dict) -> dict:
    """방호원님 적재 뷰어(loading_viewer.html)가 읽는 공통 포맷 (packing_sample.json)."""
    t = result["truck"]
    return {
        "order_id": order_id, "unit": "mm",
        "axis_note": "x=적재함 길이(0=운전석 쪽, 커질수록 뒷문 쪽), y=폭, z=높이(바닥=0). 좌표는 상자의 '모서리(최솟값 꼭짓점)' 기준",
        "container": {k: t[k] for k in ("name", "length", "width", "height", "max_weight_kg")},
        "items": [{k: p[k] for k in ("item_id", "name", "x", "y", "z", "length", "width", "height", "weight_kg",
                                     "load_order")} for p in result["placements"]],
    }
