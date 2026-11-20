"""
사용자 · 조직 테이블: 로그인 계정(화주/차주/관제), 화주 기업, 차주(기사 · 차량 · 현재 위치), 로그인 토큰.

차주 위치는 PostGIS geography(POINT) 로 저장 → 매칭 1단계 반경 필터(ST_DWithin)에 쓴다.
"""

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.orm.models import Base


class UserRow(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(120), primary_key=True)
    id: Mapped[str] = mapped_column(String(20), unique=True)
    role: Mapped[str] = mapped_column(String(10), index=True)        # shipper · driver · admin
    name: Mapped[str] = mapped_column(String(60))
    org_id: Mapped[str | None] = mapped_column(String(20))           # 화주 S-xxx · 차주 D-xxx
    created_at: Mapped[datetime] = mapped_column(DateTime)
    data: Mapped[dict] = mapped_column(JSONB)                        # phone · sub · salt · pw(해시) · documents


class ShipperRow(Base):
    __tablename__ = "shippers"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSONB)                        # biz_no 등


class DriverRow(Base):
    __tablename__ = "drivers"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    truck_type: Mapped[str] = mapped_column(String(20), index=True)
    location: Mapped[str] = mapped_column(String(80))                # 대기 지역 (주소)
    geom = mapped_column(Geography("POINT", srid=4326), nullable=True)  # 대기 지역 좌표 (PostGIS)
    month_runs: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSONB)                        # phone · plate 등


class AuthTokenRow(Base):
    __tablename__ = "auth_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(120), index=True)
