"""
적재 물리 안전 규칙 점검 — 규칙 R0~R10 하나씩 '일부러 어긴 화물'을 넣어 막히는지 (서버 없이 · 몇 초)
    실행: 수정본 폴더에서  python tests/test_packing_rules.py   (pytest 가 tests/test_suites.py 로 함께 실행)

test_packing.py 는 '정상 화물이 잘 실리는지'를 본다. 이 파일은 반대로 '위반 화물이 막히는지'와 경계값을 본다.

A. 엔진 (규칙 + EP 배치) — 위반 화물은 싣지 않거나(미적재 사유에 규칙 번호) 결과 판정이 fail
   적재 공간(R1) · 세움 고정(R3) · 중량(R4 · R9 안전율 95%) · 지지면(R5) · 상부 허용하중 90%(R6) ·
   적층 불가(R6) · 파손주의 · 냉동 최상단(R7) · 하차 순서 LIFO(R8) · 냉장 호환(R0) · 축중량(R10)
   경계값: 한도 '정확히'는 실리고, 한도를 조금이라도 넘으면 막힌다
B. 검증기 (결과 좌표만 보고 다시 채점) — 손으로 만든 잘못된 좌표를 규칙마다 잡아내는지
   엔진이 위반 배치를 만들지 않으므로, 검증기가 정말 위반을 잡는지는 이렇게 따로 확인해야 한다
"""

from _check import check, finish, section

from backend.engines.packer.engine import Options
from backend.engines.packer.models import ItemSpec
from backend.engines.packer.service import plan
from backend.engines.packer.trucks import TRUCKS
from backend.engines.packer.validate import validate

T1, T5W, T11 = TRUCKS["1톤 카고"], TRUCKS["5톤 윙바디"], TRUCKS["11톤 탑차"]   # 1톤: 2850×1600×1700 · 1,000kg


def run(items: list[ItemSpec], truck: str, cold: bool = False) -> dict:
    return plan(items, truck_type=truck, cold_chain=cold, options=Options(record_steps=False))


def status(res_or_checks, key: str) -> str:
    rows = res_or_checks["constraints"] if isinstance(res_or_checks, dict) else res_or_checks
    return next(c["status"] for c in rows if c["key"] == key)


def reasons(res: dict) -> str:
    return " · ".join(sorted({u["reason"] for u in res["unplaced"]}))


def blocked(res: dict, code: str) -> bool:
    """미적재 사유에 규칙 번호(code)가 있는가."""
    return any(u["reason"].startswith(code + " ") for u in res["unplaced"])


def all_in(res: dict) -> bool:
    return not res["unplaced"] and not any(c["status"] == "fail" for c in res["constraints"])


def placed_ok(res: dict, *keys: str) -> bool:
    """전량 적재 + 지정한 규칙만 fail 아님 (한 덩어리 화물의 R10 은 아래 '발견' 항목에서 따로 본다)."""
    return not res["unplaced"] and all(status(res, k) != "fail" for k in keys)


# ====================================================================== A. 엔진
section("A-1. 적재 공간 (R1) · 세움 고정 (R3)")
r = run([ItemSpec("긴 철재", 9200, 500, 500, 300, upright=False)], "11톤 탑차")
check("적재함(9.1m)보다 긴 화물 → 미적재 R1", blocked(r, "R1"), reasons(r))
r = run([ItemSpec("세운 기계", 1000, 800, 1800, 300, upright=True)], "1톤 카고")
check("세움 고정 · 높이 1.8m > 적재함 1.7m → 미적재 (눕히지 않음)", len(r["unplaced"]) == 1, reasons(r))
r = run([ItemSpec("눕혀도 되는 파이프", 1000, 800, 1800, 300, upright=False)], "1톤 카고")
row = r["placements"][0] if r["placements"] else {}
check("세움 고정 아님 → 눕혀서 적재 (높이 ≤ 1.7m)",
      placed_ok(r, "bounds", "orientation") and row.get("height", 9999) <= T1.height,
      f"{row.get('length')}×{row.get('width')}×{row.get('height')}")
r = run([ItemSpec("딱 맞는 상자", 2850, 1600, 1000, 300)], "1톤 카고")
check("경계값: 적재함 길이 · 폭과 정확히 같은 상자 → 적재", all_in(r), reasons(r) or "전량 적재")
r = run([ItemSpec("1mm 큰 상자", 2851, 1600, 1000, 300)], "1톤 카고")
check("경계값: 1mm 긴 상자 → 미적재 R1", blocked(r, "R1"), reasons(r))

section("A-2. 중량 (R4 최대 적재중량 · R9 안전율 95%)  — 1톤 카고 1,000kg")
r = run([ItemSpec("중량물", 500, 500, 500, 950)], "1톤 카고")
check("경계값: 950kg (= 1,000kg × 95%) → 적재 (R4 · R9 fail 아님)", placed_ok(r, "weight", "safety"),
      reasons(r) or f"전량 적재 · R9 {status(r, 'safety')}")
r = run([ItemSpec("중량물", 500, 500, 500, 951)], "1톤 카고")
check("경계값: 951kg (안전율 한도 1kg 초과) → 미적재 R9", blocked(r, "R9"), reasons(r))
r = run([ItemSpec("중량물", 500, 500, 500, 1001)], "1톤 카고")
check("1,001kg (최대 적재중량 초과) → 미적재", len(r["unplaced"]) == 1, reasons(r))
r = run([ItemSpec("중량물", 500, 500, 500, 400, qty=3)], "1톤 카고")
check("400kg × 3 = 1,200kg → 2개만 적재 · 세 번째는 중량 규칙으로 미적재",
      len(r["placements"]) == 2 and (blocked(r, "R4") or blocked(r, "R9")) and r["metrics"]["loaded_weight"] <= 950,
      f"적재 {len(r['placements'])}개 · {r['metrics']['loaded_weight']:,.0f}kg · {reasons(r)}")

section("A-3. 지지면 (R5 · 바닥 면적 80% 이상이 받쳐져야)")
# 받침은 나중에 내리는 화물(drop 2)이라 먼저 바닥에 놓이고, 폭 전체 상자는 바닥에 자리가 없어 받침 위(50% 지지)뿐
r = run([ItemSpec("받침 (폭 절반)", 2850, 800, 800, 100, drop=2),
         ItemSpec("넓은 상자 (폭 전체)", 2850, 1600, 800, 100, drop=1)], "1톤 카고")
check("폭 전체 상자를 폭 절반 받침 위에(지지 50%) → 공중 부양 위험으로 미적재 R5", blocked(r, "R5"), reasons(r))
check("결과 좌표에 지지 부족 상자 없음 (R5 pass)", status(r, "support") == "pass")

section("A-4. 상부 허용하중 90% (R6) · 적층 불가 (R6) · 최상단 전용 (R7)")
FLOOR = (2850, 1600, 800)   # 1톤 카고 바닥을 꽉 채우는 상자 → 두 번째 상자는 위에만 놓을 수 있다


def stack(base: ItemSpec, top_kg: float) -> dict:
    return run([base, ItemSpec("위 상자", *FLOOR, top_kg)], "1톤 카고")


base = ItemSpec("아래 상자 (상부 허용 100kg)", *FLOOR, 100, max_top_load=100)
r = stack(base, 90)
check("경계값: 위 90kg = 허용 100kg 의 90% → 적재", all_in(r), reasons(r) or "전량 적재")
r = stack(base, 91)
check("경계값: 위 91kg (90% 한도 초과) → 미적재 R6", blocked(r, "R6"), reasons(r))
for label, kw, code in (("적층 불가", {"stackable": False}, "R6"), ("파손주의", {"fragile": True}, "R7"),
                        ("냉동", {"frozen": True}, "R7")):
    r = stack(ItemSpec(label, *FLOOR, 100, **kw), 10)
    sp = next((p for p in r["placements"] if p["name"] == label), None)
    check(f"{label} + 일반 상자 → 일반 상자를 아래에, {label}을(를) 위에 (둘 다 적재)",
          placed_ok(r, "stacking", "top_only") and sp is not None and sp["z"] > 0, f"{label} z={sp['z'] if sp else '-'}")
    r = run([ItemSpec(label, *FLOOR, 100, qty=2, **kw)], "1톤 카고")
    check(f"{label} 2개 (바닥에 1개만 들어감) → 두 번째는 위에 못 올려 미적재 {code}",
          len(r["placements"]) == 1 and (blocked(r, "R6") or blocked(r, "R7")), reasons(r))
r = run([ItemSpec("일반", 1400, 1600, 800, 100), ItemSpec("파손주의", 1400, 1600, 800, 100, fragile=True),
         ItemSpec("일반2", 1400, 1600, 800, 50)], "1톤 카고")
fr = [p for p in r["placements"] if p["fragile"]]
check("파손주의는 최상단에 (위에 아무것도 없음 · R7 pass)", status(r, "top_only") == "pass" and fr,
      f"파손주의 z={fr[0]['z'] if fr else '-'}")

section("A-5. 하차 순서 LIFO (R8) — 먼저 내릴 화물이 뒷문 쪽 · 위쪽")
items = [ItemSpec("1차 하차", 1000, 1000, 1000, 200, qty=4, drop=1),
         ItemSpec("2차 하차", 1000, 1000, 1000, 200, qty=4, drop=2),
         ItemSpec("3차 하차", 1000, 1000, 1000, 200, qty=4, drop=3)]
r = run(items, "11톤 탑차")
rows = r["placements"]
check("11톤 탑차(뒷문 하역) 전량 적재 · LIFO 검사 통과", all_in(r) and status(r, "lifo") == "pass")


def blocks(later: dict, first: dict, side_access: bool) -> bool:
    ov = lambda a0, a1, b0, b1: min(a1, b1) - max(a0, b0) > 0  # noqa: E731
    xy = ov(first["x"], first["x"] + first["length"], later["x"], later["x"] + later["length"]) and \
        ov(first["y"], first["y"] + first["width"], later["y"], later["y"] + later["width"])
    yz = ov(first["y"], first["y"] + first["width"], later["y"], later["y"] + later["width"]) and \
        ov(first["z"], first["z"] + first["height"], later["z"], later["z"] + later["height"])
    on_top = xy and later["z"] >= first["z"] + first["height"]
    at_door = yz and later["x"] >= first["x"] + first["length"]
    return on_top or (not side_access and at_door)


bad = [(a["item_id"], b["item_id"]) for a in rows for b in rows if a["drop"] < b["drop"] and blocks(b, a, False)]
check("직접 좌표 확인: 나중 화물이 먼저 화물 위 · 뒷문 쪽을 막는 경우 0건", not bad, f"{len(bad)}쌍")
door = {d: max(p["x"] + p["length"] for p in rows if p["drop"] == d) for d in (1, 2, 3)}
check("먼저 내릴 화물일수록 뒷문(x 큰 쪽)에 가까움", door[1] >= door[2] >= door[3], f"뒷문 쪽 끝 x {door}")
r = run(items, "5톤 윙바디")
check("5톤 윙바디(측면 하역): 위에 얹힘만 금지 · LIFO 통과",
      all_in(r) and not [1 for a in r["placements"] for b in r["placements"]
                         if a["drop"] < b["drop"] and blocks(b, a, True)])

section("A-6. 냉장 호환 (R0) · 축중량 균형 (R10)")
r = run([ItemSpec("냉장 식품", 1000, 1000, 1000, 200)], "11톤 탑차", cold=True)
check("냉장 화물 + 일반 탑차 → R0 fail (등록 불가)", status(r, "compat") == "fail")
r = run([ItemSpec("냉장 식품", 1000, 1000, 1000, 200)], "11톤 냉동탑차", cold=True)
check("냉장 화물 + 냉동탑차 → R0 pass", status(r, "compat") == "pass")
r = run([ItemSpec("팔레트", 1100, 1100, 1200, 700, qty=10)], "11톤 탑차")
ab = next(c for c in r["constraints"] if c["key"] == "cog")["axle"]
check("고르게 실은 팔레트 10개 → R10 fail 아님 · 좌우 편차 15% 이하", status(r, "cog") != "fail" and ab["lateral"] <= 0.15,
      f"전후 {ab['longitudinal']:.0%} · 좌우 {ab['lateral']:.1%}")

section("A-7. 폭을 다 채우지 않는 한 덩어리 화물 — 전체 이동으로 축중량 균형 (R10)")
# 예전: 운전석 쪽 왼쪽 구석(0, 0)에 놓여 R10 fail → 차종 자동 추천이 5톤 · 25톤으로 커졌다
for label, item, truck, smallest in (
        ("1톤 카고 · 950kg 상자 1개", ItemSpec("중량물", 500, 500, 500, 950), "1톤 카고", "1톤 카고"),
        ("11톤 탑차 · 4,000kg 강괴 1개", ItemSpec("강괴", 600, 600, 600, 4000), "11톤 탑차", "5톤 윙바디")):
    r = run([item], truck)
    p, ab = r["placements"][0], next(c for c in r["constraints"] if c["key"] == "cog")["axle"]
    check(f"{label} → 가운데 · 축 사이로 옮겨 R10 pass", status(r, "cog") == "pass",
          f"위치 x={p['x']} y={p['y']} · 전후 {ab['longitudinal']:.0%} · 좌우 편차 {ab['lateral']:.1%}")
    check(f"{label} → 옮긴 뒤에도 다른 규칙 모두 통과", all_in(r))
    auto = plan([item], truck_type=None, cold_chain=False, options=Options(record_steps=False))
    check(f"{label} → 차종 자동 추천이 가장 작은 적합 차량 ({smallest})", auto["truck"]["name"] == smallest,
          auto["truck"]["name"])
r = run([ItemSpec("1차 하차", 600, 600, 600, 300, drop=1), ItemSpec("2차 하차", 600, 600, 600, 300, drop=2)], "11톤 탑차")
check("여러 화물을 함께 옮겨도 하차 순서(LIFO) · 겹침 · 지지면 유지", all_in(r) and status(r, "cog") == "pass"
      and not [1 for a in r["placements"] for b in r["placements"] if a["drop"] < b["drop"] and blocks(b, a, False)],
      " · ".join(f"{q['name']} x={q['x']}" for q in r["placements"]))


# ====================================================================== B. 검증기
def box(i, x, y, z, l, w, h, kg, **kw) -> dict:
    return {"item_id": f"B{i}", "x": x, "y": y, "z": z, "length": l, "width": w, "height": h, "weight_kg": kg,
            "orig": kw.pop("orig", [l, w, h]), "upright": kw.pop("upright", False),
            "stackable": kw.pop("stackable", True), "max_top_load": kw.pop("max_top_load", None),
            "drop": kw.pop("drop", 1), "fragile": kw.pop("fragile", False), "frozen": kw.pop("frozen", False)}


def v(rows, truck=T11, **kw) -> list[dict]:
    return validate(truck, rows, min_support=0.8, lifo=True, safety_margin=0.05, top_load_margin=0.10, **kw)


def vcheck(name, rows, key, want, truck=T11, **kw):
    got = status(v(rows, truck, **kw), key)
    check(name, got == want, f"{key} = {got}")


section("B. 검증기 — 손으로 만든 잘못된 좌표를 규칙마다 잡아내는지")
ok_box = box(1, 0, 0, 0, 1000, 1000, 1000, 100)
vcheck("R0 냉장 화물 + 일반 탑차 → fail", [ok_box], "compat", "fail", cold_chain=True)
vcheck("R1 적재함 밖으로 1mm 돌출 → fail", [box(1, 8101, 0, 0, 1000, 1000, 1000, 100)], "bounds", "fail")
vcheck("R1 경계에 딱 맞음 → pass", [box(1, 8100, 1350, 1500, 1000, 1000, 1000, 100)], "bounds", "pass")
vcheck("R2 상자 두 개가 1mm 겹침 → fail", [ok_box, box(2, 999, 0, 0, 1000, 1000, 1000, 100)], "overlap", "fail")
vcheck("R2 면이 맞닿기만 함 → pass", [ok_box, box(2, 1000, 0, 0, 1000, 1000, 1000, 100)], "overlap", "pass")
vcheck("R3 세움 고정 품목이 눕혀짐 → fail", [box(1, 0, 0, 0, 1800, 1000, 500, 100, orig=[1000, 500, 1800],
                                                upright=True)], "orientation", "fail")
vcheck("R4 총중량 11,001kg (11톤 탑차) → fail",
       [box(1, 0, 0, 0, 1000, 1000, 1000, 5500.5), box(2, 1000, 0, 0, 1000, 1000, 1000, 5500.5)], "weight", "fail")
vcheck("R5 아래에 아무것도 없이 공중에 뜬 상자 → fail", [box(1, 0, 0, 500, 1000, 1000, 1000, 100)], "support", "fail")
vcheck("R5 바닥 면적 50%만 받쳐짐 → fail",
       [ok_box, box(2, 500, 0, 1000, 1000, 1000, 500, 50)], "support", "fail")
vcheck("R5 바닥 면적 80% 받쳐짐 (경계) → pass",
       [ok_box, box(2, 200, 0, 1000, 1000, 1000, 500, 50)], "support", "pass")
cap = dict(max_top_load=100)
vcheck("R6 위 하중 91kg > 허용 100kg 의 90% → fail", [box(1, 0, 0, 0, 1000, 1000, 1000, 50, **cap),
                                                    box(2, 0, 0, 1000, 1000, 1000, 500, 91)], "stacking", "fail")
vcheck("R6 위 하중 90kg = 90% (경계) → pass", [box(1, 0, 0, 0, 1000, 1000, 1000, 50, **cap),
                                             box(2, 0, 0, 1000, 1000, 1000, 500, 90)], "stacking", "pass")
vcheck("R6 하중이 두 단 아래까지 전달 (위 50 + 가운데 45 = 95kg) → 맨 아래 fail",
       [box(1, 0, 0, 0, 1000, 1000, 500, 50, **cap), box(2, 0, 0, 500, 1000, 1000, 500, 45),
        box(3, 0, 0, 1000, 1000, 1000, 500, 50)], "stacking", "fail")
vcheck("R6 적층 불가 상자 위에 1kg → fail", [box(1, 0, 0, 0, 1000, 1000, 1000, 50, stackable=False),
                                       box(2, 0, 0, 1000, 1000, 1000, 500, 1)], "stacking", "fail")
vcheck("R7 파손주의 상자 위에 다른 상자 → fail", [box(1, 0, 0, 0, 1000, 1000, 1000, 50, fragile=True),
                                            box(2, 0, 0, 1000, 1000, 1000, 500, 1)], "top_only", "fail")
vcheck("R7 냉동 상자 위에 다른 상자 → fail", [box(1, 0, 0, 0, 1000, 1000, 1000, 50, frozen=True),
                                          box(2, 0, 0, 1000, 1000, 1000, 500, 1)], "top_only", "fail")
first_bottom = [box(1, 0, 0, 0, 1000, 1000, 1000, 50, drop=1), box(2, 0, 0, 1000, 1000, 1000, 1000, 50, drop=2)]
vcheck("R8 먼저 내릴 화물 위에 나중 화물 → fail", first_bottom, "lifo", "fail")
door_block = [box(1, 0, 0, 0, 1000, 1000, 1000, 50, drop=1), box(2, 1000, 0, 0, 1000, 1000, 1000, 50, drop=2)]
vcheck("R8 나중 화물이 뒷문 쪽에서 먼저 화물을 막음 (탑차) → fail", door_block, "lifo", "fail")
vcheck("R8 같은 배치라도 측면 하역 차량(윙바디) → pass", door_block, "lifo", "pass", truck=T5W)
vcheck("R8 측면 하역 차량이라도 위에 얹힘은 → fail", first_bottom, "lifo", "fail", truck=T5W)


def util_rows(kg: float) -> list[dict]:   # 11톤 탑차 바닥 가운데에 중량만 다르게
    return [box(1, 4050, 675, 0, 1000, 1000, 1000, kg)]


vcheck("R9 중량 사용률 96% → fail (안전율 5%)", util_rows(10_560), "safety", "fail")
vcheck("R9 중량 사용률 92% → warn (90~95% 위험 구간)", util_rows(10_120), "safety", "warn")
vcheck("R9 중량 사용률 95% (경계) → fail 아님", util_rows(10_450), "safety", "warn")
half = T11.width // 2
vcheck("R10 좌우 무게중심 편차 16% → fail",
       [box(1, 4050, half + int(0.16 * T11.width) - 500, 0, 1000, 1000, 1000, 6000)], "cog", "fail")
vcheck("R10 좌우 편차 12% → warn",
       [box(1, 4050, half + int(0.12 * T11.width) - 500, 0, 1000, 1000, 1000, 6000)], "cog", "warn")
vcheck("R10 전후 무게중심 10% (운전석 쪽 쏠림) → fail",
       [box(1, 410, half - 500, 0, 1000, 1000, 1000, 6000)], "cog", "fail")
vcheck("R10 같은 쏠림이라도 경량 화물(최대 적재량 30% 미만) → warn 으로 완화",
       [box(1, 410, half - 500, 0, 1000, 1000, 1000, 1000)], "cog", "warn")
vcheck("R10 가운데 · 고르게 → pass", [box(1, 3550, half - 500, 0, 2000, 1000, 1000, 6000)], "cog", "pass")

finish()
