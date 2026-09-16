from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminExport(Base):
    __tablename__ = "admin_exports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    kind: Mapped[str] = mapped_column(String(32), default="convokit")
    status: Mapped[str] = mapped_column(String(16), default="started")
    range_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    range_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    utterance_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speaker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ModerationSettings(Base):
    """Console-editable moderation config. One row, id=1.

    `thresholds` holds only the categories the console has written; they are
    merged over the built-in MODERATION_VALUES_FOR_BLOCKED, so a category the
    OpenAI API adds later still has a value without a migration.
    """

    __tablename__ = "moderation_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_moderation_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    thresholds: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
