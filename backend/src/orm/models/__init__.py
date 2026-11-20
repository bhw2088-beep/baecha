"""DB SQLAlchemy ORM 모델 (PostgreSQL + PostGIS). 테이블 정의는 user.py · order.py."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
