from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
