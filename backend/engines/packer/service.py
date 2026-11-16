"""
빈패킹 서비스: 입력 품목 → (차량 자동 추천) → EP 엔진 → 검증 → (선택) py3dbp 비교.
라우터와 화물 등록 로직은 이 함수(plan)만 호출한다.
"""

from .py3dbp_engine import run_py3dbp
from .engine import EPPacker, Options, summarize
from .models import ItemSpec, expand
from .trucks import TRUCKS, compatible_trucks
from .validate import validate

MAX_BOXES = 300


class PackingError(ValueError):
    pass


def _run(container, boxes, options: Options, cold_chain: bool) -> dict:
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
                                cold_chain=cold_chain),
    }


def plan(items: list[ItemSpec], *, truck_type: str | None, cold_chain: bool, options: Options,
         compare_baseline: bool = False) -> dict:
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
            # 사전 검사: 총중량/총부피가 이미 차량 한계를 넘으면 시뮬레이션 생략
            if total_w > t.max_weight:
                row["note"] = "총중량 초과"
            elif total_v > t.volume:
                row["note"] = "총부피 초과"
            elif chosen is not None:
                row["note"] = "더 작은 차량으로 충분"
            else:
                r = _run(t, boxes, options, cold_chain)
                m = r["metrics"]
                row.update(evaluated=True, placed=m["placed"], volume_util=m["volume_util"],
                           weight_util=m["weight_util"], fits=m["unplaced"] == 0,
                           note="전량 적재" if m["unplaced"] == 0 else f"{m['unplaced']}개 미적재")
                if m["unplaced"] == 0:
                    chosen = r
                if best is None or m["placed"] > best["metrics"]["placed"]:
                    best = r
            candidates.append(row)
        chosen = chosen or best
        if chosen is None:
            raise PackingError("냉장/중량/부피 조건을 만족하는 차량이 없습니다. 품목을 나누어 등록해 주세요.")

    out = {**chosen, "auto": truck_type is None, "candidates": candidates,
           "options": vars(options), "cold_chain": cold_chain}
    if compare_baseline:
        container = TRUCKS[chosen["truck"]["name"]]
        b = run_py3dbp(container, boxes)
        out["baseline"] = {
            "name": "py3dbp 1.1.2 (pivot heuristic)",
            "placements": b["placements"], "unplaced": b["unplaced"],
            "metrics": {**summarize(container, b["placements"], len(boxes), total_w), "elapsed_ms": b["elapsed_ms"]},
            "constraints": validate(container, b["placements"], min_support=options.min_support,
                                    lifo=options.lifo, cold_chain=cold_chain),
        }
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
