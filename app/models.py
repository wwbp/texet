from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
