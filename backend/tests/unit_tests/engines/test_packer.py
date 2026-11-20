"""3D 적재 엔진 (engines/packer) 단위 테스트 — 품목 펼치기 · EP 배치 · 차종 자동 추천 · 규칙 재채점."""

import pytest

from src.engines.packer.engine import EPPacker, Options
from src.engines.packer.models import Box, ItemSpec, expand
from src.engines.packer.samples import SAMPLES
from src.engines.packer.service import MAX_BOXES, PackingError, plan
from src.engines.packer.trucks import TRUCKS, Container, compatible_trucks
from src.engines.packer.validate import validate

NO_STEPS = Options(record_steps=False)
VALIDATION_KEYS = {"compat", "bounds", "overlap", "support", "orientation", "stacking", "weight", "lifo", "top_only",
                   "safety", "cog"}


def _overlaps(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["length"] and b["x"] < a["x"] + a["length"]
            and a["y"] < b["y"] + b["width"] and b["y"] < a["y"] + a["width"]
            and a["z"] < b["z"] + b["height"] and b["z"] < a["z"] + a["height"])


def _sample_items(name: str) -> list[ItemSpec]:
    return [ItemSpec(**i) for i in SAMPLES[name]["items"]]


def _row(item_id, x, y, z, l, w, h, kg=10, **kw):
    return dict(item_id=item_id, x=x, y=y, z=z, length=l, width=w, height=h, weight_kg=kg, orig=[l, w, h],
                upright=True, stackable=True, max_top_load=None, drop=1, **kw)


# ---------------------------------------------------------------- 생성자 · 데이터 구조
def test_ctor_itemspec_defaults():
    it = ItemSpec("상자", 1000, 800, 600, 25.0)
    assert (it.qty, it.upright, it.stackable, it.max_top_load, it.drop) == (1, True, True, None, 1)
    assert not it.fragile and not it.frozen


def test_ctor_options_defaults():
    o = Options()
    assert o.merit == "wall" and o.min_support == 0.8 and o.lifo is True and o.balance is True
    assert o.safety_margin == 0.0 and o.top_load_margin == 0.0


def test_ctor_container_volume_and_dict():
    c = Container("테스트", 1000, 2000, 3000, 500)
    assert c.volume == 6_000_000_000
    d = c.as_dict()
    assert d["volume_m3"] == 6.0 and d["max_weight_kg"] == 500 and d["refrigerated"] is False


def test_ctor_box_orientations_upright_keeps_height():
    b = Box("S01-001", 0, "x", 300, 200, 100, 1.0, upright=True, stackable=True, cap=0, drop=1)
    assert b.volume == 6_000_000
    assert all(o[2] == 100 for o in b.orientations()) and len(b.orientations()) == 2


def test_ctor_box_orientations_free_rotation():
    b = Box("S01-001", 0, "x", 300, 200, 100, 1.0, upright=False, stackable=True, cap=0, drop=1)
    assert len(b.orientations()) == 6


def test_ctor_packer_accepts_container():
    p = EPPacker(TRUCKS["1톤 카고"], NO_STEPS)
    assert p is not None


# ---------------------------------------------------------------- expand (품목 → 상자)
def test_expand_smoke():
    boxes = expand([ItemSpec("A", 100, 100, 100, 5, qty=3), ItemSpec("B", 200, 200, 200, 9)])
    assert len(boxes) == 4 and all(isinstance(b, Box) for b in boxes)
    assert [b.uid for b in boxes] == ["S01-001", "S01-002", "S01-003", "S02-001"]


def test_expand_actual_top_only_items_cannot_carry_load():
    fragile, frozen, normal, capped = expand([
        ItemSpec("유리", 100, 100, 100, 1, fragile=True), ItemSpec("냉동", 100, 100, 100, 1, frozen=True),
        ItemSpec("일반", 100, 100, 100, 1), ItemSpec("제한", 100, 100, 100, 1, max_top_load=30)])
    assert fragile.cap == 0 and not fragile.stackable
    assert frozen.cap == 0 and not frozen.stackable
    assert normal.cap == float("inf") and normal.stackable
    assert capped.cap == 30.0


# ---------------------------------------------------------------- compatible_trucks
def test_compatible_trucks_smoke():
    trucks = compatible_trucks(cold_chain=False)
    assert trucks and all(isinstance(t, Container) for t in trucks)


def test_compatible_trucks_actual_cold_chain_only_refrigerated_sorted_small_first():
    cold = compatible_trucks(cold_chain=True)
    assert cold and all(t.refrigerated for t in cold)
    assert all(not t.refrigerated for t in compatible_trucks(cold_chain=False))
    assert [t.volume for t in cold] == sorted(t.volume for t in cold)


# ---------------------------------------------------------------- plan (서비스 진입점)
def test_plan_smoke():
    r = plan(_sample_items("팔레트 혼적 (단일 하차지)"), truck_type=None, cold_chain=False, options=NO_STEPS)
    assert {"truck", "placements", "unplaced", "metrics", "constraints", "candidates", "auto",
            "required_tonnage"} <= r.keys()
    assert r["auto"] is True and r["metrics"]["placed"] == len(r["placements"])
    assert {c["key"] for c in r["constraints"]} == VALIDATION_KEYS


@pytest.mark.parametrize("name", list(SAMPLES))
def test_plan_actual_sample_fits_without_violation(name):
    smp = SAMPLES[name]
    r = plan(_sample_items(name), truck_type=None, cold_chain=smp["cold_chain"], options=NO_STEPS)
    truck = r["truck"]
    assert not r["unplaced"], f"{truck['name']} 에 미적재 {len(r['unplaced'])}개"
    assert [c["name"] for c in r["constraints"] if c["status"] == "fail"] == []
    assert truck["refrigerated"] or not smp["cold_chain"]
    rows = r["placements"]
    out = [x["item_id"] for x in rows if x["x"] + x["length"] > truck["length"] or x["y"] + x["width"] > truck["width"]
           or x["z"] + x["height"] > truck["height"] or min(x["x"], x["y"], x["z"]) < 0]
    assert out == [], "적재함 밖으로 나간 상자"
    hits = [(a["item_id"], b["item_id"]) for i, a in enumerate(rows) for b in rows[i + 1:] if _overlaps(a, b)]
    assert hits == [], "상자끼리 겹침"


@pytest.mark.parametrize("name", list(SAMPLES))
def test_plan_actual_auto_picks_smallest_fitting_truck(name):
    smp = SAMPLES[name]
    items = _sample_items(name)
    chosen = plan(items, truck_type=None, cold_chain=smp["cold_chain"], options=NO_STEPS)["truck"]["name"]
    smaller = [t for t in compatible_trucks(smp["cold_chain"]) if t.volume < TRUCKS[chosen].volume]
    if not smaller:
        pytest.skip("더 작은 차종 없음")
    r = plan(items, truck_type=smaller[-1].name, cold_chain=smp["cold_chain"], options=NO_STEPS)
    assert r["unplaced"] or any(c["status"] == "fail" for c in r["constraints"]), \
        f"{smaller[-1].name} 에도 다 실리는데 {chosen} 을 추천함"


def test_plan_actual_fragile_and_frozen_stay_on_top():
    r = plan([ItemSpec("팔레트", 1100, 1100, 1000, 300, qty=4), ItemSpec("유리", 800, 600, 500, 40, qty=3, fragile=True),
              ItemSpec("냉동", 600, 400, 300, 15, qty=4, frozen=True)],
             truck_type="11톤 냉동탑차", cold_chain=True, options=NO_STEPS)
    top = next(c for c in r["constraints"] if c["key"] == "top_only")
    assert top["status"] == "pass" and not r["unplaced"]


def test_plan_actual_rejects_empty_unknown_truck_and_too_many_boxes():
    with pytest.raises(PackingError):
        plan([], truck_type=None, cold_chain=False, options=NO_STEPS)
    with pytest.raises(PackingError, match="차종"):
        plan([ItemSpec("A", 100, 100, 100, 1)], truck_type="99톤 트럭", cold_chain=False, options=NO_STEPS)
    with pytest.raises(PackingError, match=str(MAX_BOXES)):
        plan([ItemSpec("A", 100, 100, 100, 1, qty=MAX_BOXES + 1)], truck_type=None, cold_chain=False,
             options=NO_STEPS)


def test_plan_actual_overweight_cargo_has_no_truck():
    with pytest.raises(PackingError):
        plan([ItemSpec("철근", 1000, 1000, 500, 30_000)], truck_type=None, cold_chain=False, options=NO_STEPS)


# ---------------------------------------------------------------- validate (규칙 재채점)
def test_validate_smoke():
    checks = validate(TRUCKS["5톤 윙바디"], [_row("A", 0, 0, 0, 1000, 1000, 1000)], min_support=0.8, lifo=False)
    assert {c["key"] for c in checks} == VALIDATION_KEYS
    assert all(c["status"] in ("pass", "warn", "fail") for c in checks)


def test_validate_actual_detects_overlap_and_out_of_bounds():
    t = TRUCKS["1톤 카고"]
    rows = [_row("A", 0, 0, 0, 1000, 1000, 1000), _row("B", 500, 500, 0, 1000, 1000, 1000),
            _row("C", t.length - 100, 0, 0, 1000, 1000, 1000)]
    by = {c["key"]: c for c in validate(t, rows, min_support=0.8, lifo=False)}
    assert by["overlap"]["status"] == "fail"
    assert by["bounds"]["status"] == "fail" and "C" in by["bounds"]["violations"]


def test_validate_actual_box_on_frozen_is_violation():
    rows = [_row("A", 0, 0, 0, 1000, 1000, 500, frozen=True), _row("B", 0, 0, 500, 1000, 1000, 500)]
    chk = next(c for c in validate(TRUCKS["5톤 냉동탑차"], rows, min_support=0.8, lifo=False, cold_chain=True)
               if c["key"] == "top_only")
    assert chk["status"] == "fail"


def test_validate_actual_cold_chain_on_dry_truck_fails_compat():
    chk = next(c for c in validate(TRUCKS["5톤 윙바디"], [], min_support=0.8, lifo=False, cold_chain=True)
               if c["key"] == "compat")
    assert chk["status"] == "fail"
