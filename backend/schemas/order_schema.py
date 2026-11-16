"""화물 등록(3D 적재 품목 포함) · 운행 · 매칭 관제 요청 스키마."""

from typing import Literal

from pydantic import BaseModel, Field


class PackItemIn(BaseModel):
    """적재 품목 한 줄 (mm, kg)."""
    name: str = Field(min_length=1, max_length=40)
    length: int = Field(ge=50, le=13_000)
    width: int = Field(ge=50, le=2_500)
    height: int = Field(ge=50, le=3_000)
    weight: float = Field(gt=0, le=25_000)
    qty: int = Field(1, ge=1, le=300)
    upright: bool = True
    stackable: bool = True
    max_top_load: float | None = Field(None, ge=0, le=50_000)
    drop: int = Field(1, ge=1, le=9)


class PackOptionsIn(BaseModel):
    merit: Literal["wall", "floor"] = "wall"
    sort: Literal["area_height", "volume", "weight"] = "area_height"
    min_support: float = Field(0.8, ge=0.5, le=1.0)
    lifo: bool = True

    def to_engine(self, record_steps: bool = True):
        from ..engines.packer.engine import Options
        return Options(merit=self.merit, sort=self.sort, min_support=self.min_support, lifo=self.lifo,
                       record_steps=record_steps)


def _specs(items: list[PackItemIn]):
    from ..engines.packer.models import ItemSpec
    return [ItemSpec(**i.model_dump()) for i in items]


class PackPlanIn(BaseModel):
    items: list[PackItemIn] = Field(min_length=1, max_length=50)
    truck_type: str | None = None  # None = 자동 추천
    cold_chain: bool = False
    options: PackOptionsIn = PackOptionsIn()
    compare_baseline: bool = False

    def item_specs(self):
        return _specs(self.items)


class OrderIn(BaseModel):
    title: str = Field(min_length=2, max_length=60)
    truck_type: str
    origin: str = Field(min_length=2)
    destination: str = Field(min_length=2)
    price: int = Field(gt=0, le=10_000_000)
    urgent: bool = False
    cold_chain: bool = False
    temp_set: float | None = Field(None, ge=-30, le=10)
    # 적재 품목을 함께 보내면 서버가 같은 엔진으로 다시 계산해서 '전량 적재 가능'을 검증한 뒤 저장한다
    items: list[PackItemIn] | None = Field(None, max_length=50)
    packing_options: PackOptionsIn = PackOptionsIn()

    def item_specs(self):
        return _specs(self.items or [])


class CompleteIn(BaseModel):
    receiver: str = Field(min_length=1, max_length=30)


class IncentiveIn(BaseModel):
    amount: int = Field(gt=0, le=300_000)


class AssignIn(BaseModel):
    driver_id: str


class MatchSettingsIn(BaseModel):
    """관제가 바꾸는 자동 매칭 설정. 적재율 가중치 = 1 − 거리 가중치."""
    radius_km: int = Field(ge=10, le=200)
    timeout_sec: int = Field(ge=15, le=300)
    w_dist: float = Field(ge=0, le=1)
