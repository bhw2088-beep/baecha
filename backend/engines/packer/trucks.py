"""
차종별 적재함 내부 치수 (mm) · 최대 적재중량 (kg) · 냉장 여부 · 측면 개방 여부.

치수는 국내 표준형 차량의 대표값(근사치)이다. 실제 운영 시 차량 등록 정보로 교체할 것.
side_access=True 인 차량(윙바디/카고)은 측면으로도 하역할 수 있으므로
하차 순서(LIFO) 제약에서 '앞쪽(도어 방향) 가로막힘'은 따지지 않고 '위에 얹힘'만 따진다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Container:
    name: str
    length: int        # x: 0 = 운전석 쪽, length = 뒷문
    width: int         # y
    height: int        # z: 0 = 바닥
    max_weight: float  # kg
    refrigerated: bool = False
    side_access: bool = False

    @property
    def volume(self) -> int:
        return self.length * self.width * self.height

    def as_dict(self) -> dict:
        return {"name": self.name, "length": self.length, "width": self.width, "height": self.height,
                "max_weight_kg": self.max_weight, "refrigerated": self.refrigerated,
                "side_access": self.side_access, "volume_m3": round(self.volume / 1e9, 2)}


TRUCKS: dict[str, Container] = {c.name: c for c in [
    Container("1톤 카고", 2850, 1600, 1700, 1_000, side_access=True),
    Container("5톤 윙바디", 6200, 2350, 2400, 5_000, side_access=True),
    Container("5톤 냉동탑차", 5900, 2200, 2150, 4_500, refrigerated=True),
    Container("11톤 탑차", 9100, 2350, 2500, 11_000),
    Container("11톤 냉동탑차", 9000, 2250, 2300, 10_000, refrigerated=True),
    Container("25톤 무진동", 10100, 2350, 2500, 25_000),
]}


def compatible_trucks(cold_chain: bool) -> list[Container]:
    """자동 추천 후보: 냉장 화물 → 냉장 차량만, 상온 화물 → 상온 차량만(냉장차는 운임이 비쌈). 작은 차량부터."""
    trucks = [t for t in TRUCKS.values() if t.refrigerated == cold_chain]
    return sorted(trucks, key=lambda t: (t.volume, t.max_weight))
