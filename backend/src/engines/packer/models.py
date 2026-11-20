"""빈패킹 입력/출력 데이터 구조. 단위: mm, kg."""

import math
from dataclasses import dataclass, field
from itertools import permutations


@dataclass
class ItemSpec:
    """화주가 입력하는 품목(SKU) 한 줄. qty 만큼 Box 로 펼쳐진다."""
    name: str
    length: int
    width: int
    height: int
    weight: float
    qty: int = 1
    upright: bool = True              # 세움 고정(This side up): 수직축 회전만 허용
    stackable: bool = True            # False = 적층 불가(파손주의): 위에 아무것도 못 올림
    max_top_load: float | None = None  # 상부 허용하중(kg). None = 제한 없음
    drop: int = 1                     # 하차 순서 (1 = 가장 먼저 내림)
    fragile: bool = False             # 파손주의 → 최상단 배치 강제 (위에 아무것도 못 올림)
    frozen: bool = False              # 냉동 화물 → 최상단 배치 강제 (냉기 순환 · 하중 결로 방지)


@dataclass
class Box:
    uid: str
    sku: int          # 품목 번호 (색상/그룹용)
    name: str
    l: int
    w: int
    h: int
    weight: float
    upright: bool
    stackable: bool
    cap: float        # 위에 얹을 수 있는 총 하중
    drop: int
    fragile: bool = False
    frozen: bool = False

    @property
    def volume(self) -> int:
        return self.l * self.w * self.h

    def orientations(self) -> list[tuple[int, int, int]]:
        """허용 방향 (dx, dy, dz). 세움 고정이면 높이축 유지한 채 90° 회전 2가지만."""
        if self.upright:
            cands = [(self.l, self.w, self.h), (self.w, self.l, self.h)]
        else:
            cands = list(permutations((self.l, self.w, self.h)))
        out = []
        for c in cands:
            if c not in out:
                out.append(c)
        return out


def expand(items: list[ItemSpec]) -> list[Box]:
    boxes = []
    for sku, it in enumerate(items):
        top_only = it.fragile or it.frozen  # 파손주의 · 냉동은 최상단에만 → 위에 적재 금지와 같다
        if not it.stackable or top_only:
            cap = 0.0
        else:
            cap = math.inf if it.max_top_load is None else float(it.max_top_load)
        for n in range(it.qty):
            boxes.append(Box(uid=f"S{sku + 1:02d}-{n + 1:03d}", sku=sku, name=it.name, l=it.length, w=it.width,
                             h=it.height, weight=float(it.weight), upright=it.upright,
                             stackable=it.stackable and not top_only, cap=cap, drop=it.drop,
                             fragile=it.fragile, frozen=it.frozen))
    return boxes


@dataclass
class Placement:
    box: Box
    x: int
    y: int
    z: int
    dx: int
    dy: int
    dz: int
    step: int = 0
    ep: tuple[int, int, int] | None = None
    support_ratio: float = 1.0
    supporters: list[tuple[int, float]] = field(default_factory=list)  # (placement index, 하중 분담 비율)
    load: float = 0.0  # 위에서 누르는 하중 (kg)
    balanced: bool = False  # R10 좌우 균형 때문에 같은 줄 반대편에 놓았는지

    @property
    def x1(self) -> int:
        return self.x + self.dx

    @property
    def y1(self) -> int:
        return self.y + self.dy

    @property
    def z1(self) -> int:
        return self.z + self.dz

    def to_dict(self) -> dict:
        """팀 공통 적재 계획 포맷 (packing_sample.json) + 확장 필드."""
        b = self.box
        return {
            "item_id": b.uid, "name": b.name, "sku": b.sku,
            "x": self.x, "y": self.y, "z": self.z,
            "length": self.dx, "width": self.dy, "height": self.dz,
            "weight_kg": b.weight, "load_order": self.step,
            "drop": b.drop, "upright": b.upright, "stackable": b.stackable,
            "fragile": b.fragile, "frozen": b.frozen,
            "max_top_load": None if b.cap == float("inf") else b.cap,
            "orig": [b.l, b.w, b.h], "rotated": (self.dx, self.dy, self.dz) != (b.l, b.w, b.h),
            "support_ratio": round(self.support_ratio, 3), "load_on_top": round(self.load, 1),
            "ep": list(self.ep) if self.ep else None, "balanced": self.balanced,
        }
