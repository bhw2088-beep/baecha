"""
3D 적재 엔진 점검 (서버 없이).   실행: 수정본 폴더에서
    .venv\\Scripts\\python tests\\test_packing.py

- 샘플 시나리오마다 '차종 자동 추천'으로 계획을 세워 전량 적재 + 필수 제약 충족인지
- 같은 품목을 더 작은 차량에 넣으면 미적재가 생기는지 (자동 추천이 가장 작은 적합 차량을 고르는지)
- 상자끼리 겹치지 않고 적재함 밖으로 나가지 않는지 (좌표 직접 검사)
"""

from _common import check, finish, section

from backend.engines.packer.engine import Options
from backend.engines.packer.models import ItemSpec
from backend.engines.packer.samples import SAMPLES
from backend.engines.packer.service import plan
from backend.engines.packer.trucks import TRUCKS, compatible_trucks


def overlaps(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["length"] and b["x"] < a["x"] + a["length"]
            and a["y"] < b["y"] + b["width"] and b["y"] < a["y"] + a["width"]
            and a["z"] < b["z"] + b["height"] and b["z"] < a["z"] + a["height"])


for name, smp in SAMPLES.items():
    section(f"샘플 · {name}")
    items = [ItemSpec(**i) for i in smp["items"]]
    res = plan(items, truck_type=None, cold_chain=smp["cold_chain"], options=Options(record_steps=False))
    m, truck = res["metrics"], res["truck"]
    fails = [c["name"] for c in res["constraints"] if c["status"] == "fail"]
    check("전량 적재", not res["unplaced"], f"{truck['name']} · {m['placed']}/{m['total']}개")
    check("필수 제약 위반 없음", not fails, ", ".join(fails))
    check("냉장 화물이면 냉장 차량", truck["refrigerated"] or not smp["cold_chain"], truck["name"])

    rows = res["placements"]
    out = [r["item_id"] for r in rows
           if r["x"] + r["length"] > truck["length"] or r["y"] + r["width"] > truck["width"]
           or r["z"] + r["height"] > truck["height"] or min(r["x"], r["y"], r["z"]) < 0]
    check("적재함 경계 안", not out, f"밖으로 나간 상자 {len(out)}개" if out else "")
    hit = sum(1 for i in range(len(rows)) for j in range(i + 1, len(rows)) if overlaps(rows[i], rows[j]))
    check("상자끼리 겹치지 않음", hit == 0, f"겹침 {hit}쌍" if hit else "")

    smaller = [t for t in compatible_trucks(smp["cold_chain"]) if t.volume < TRUCKS[truck["name"]].volume]
    if smaller:
        t = smaller[-1]
        r2 = plan(items, truck_type=t.name, cold_chain=smp["cold_chain"], options=Options(record_steps=False))
        bad = r2["unplaced"] or any(c["status"] == "fail" for c in r2["constraints"])
        check("한 단계 작은 차량에는 안 맞음 (가장 작은 적합 차량 추천)", bool(bad), t.name)

finish()
