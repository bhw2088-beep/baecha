"""
py3dbp (오픈소스, enzoruiz/3dbinpacking) 기준선(baseline) 실행기.

py3dbp 는 '이미 놓인 상자의 모서리(pivot)'만 후보로 쓰는 단순 휴리스틱이라 EP 의 원형에 가깝지만,
  - 투영(projection)이 없어 빈틈을 놓치고
  - 지지면·세움 고정·적층 하중·하차 순서 같은 현장 제약을 검사하지 않으며
  - put_item 이 '경계 안에 들어가는 첫 회전'만 시도하고 겹치면 바로 포기한다 (1.1.2 기준).
그래서 엔진 본체가 아니라 '비교용 기준선'으로 쓰고, 결과를 같은 검증기(validate.py)로 채점한다.

좌표 대응: py3dbp (width, height, depth) = 우리 (x 길이, z 높이, y 폭)
"""

import time

from py3dbp import Bin, Item, Packer

from .models import Box
from .trucks import Container


def run_py3dbp(container: Container, boxes: list[Box]) -> dict:
    t0 = time.perf_counter()
    packer = Packer()
    packer.add_bin(Bin(container.name, container.length, container.height, container.width, container.max_weight))
    by_uid = {b.uid: b for b in boxes}
    for b in boxes:
        packer.add_item(Item(b.uid, b.l, b.h, b.w, b.weight))
    packer.pack(bigger_first=True, number_of_decimals=0)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    rows = []
    bin_ = packer.bins[0]
    for step, it in enumerate(bin_.items, start=1):
        b = by_uid[it.name]
        dx, dz, dy = (int(v) for v in it.get_dimension())
        x, z, y = (int(v) for v in it.position)
        rows.append({
            "item_id": b.uid, "name": b.name, "sku": b.sku, "x": x, "y": y, "z": z,
            "length": dx, "width": dy, "height": dz, "weight_kg": b.weight, "load_order": step,
            "drop": b.drop, "upright": b.upright, "stackable": b.stackable,
            "max_top_load": None if b.cap == float("inf") else b.cap,
            "orig": [b.l, b.w, b.h], "rotated": (dx, dy, dz) != (b.l, b.w, b.h),
            "support_ratio": None, "load_on_top": None, "ep": [x, y, z],
        })
    unplaced = [{"item_id": it.name, "name": by_uid[it.name].name, "sku": by_uid[it.name].sku,
                 "reason": "py3dbp: 배치 실패"} for it in bin_.unfitted_items]
    return {"placements": rows, "unplaced": unplaced, "elapsed_ms": elapsed}
