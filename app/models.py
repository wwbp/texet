from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import UTTERANCE_STATUS_RECEIVED, UTTERANCE_STATUSES_SQL


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Base(DeclarativeBase):
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "ux_conversations_owner_open",
            "owner_speaker_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    owner_speaker_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("speakers.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="open")
    last_activity_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Utterance(Base):
    __tablename__ = "utterances"
    __table_args__ = (
        CheckConstraint(
            f"status in ({UTTERANCE_STATUSES_SQL})",
            name="ck_utterances_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id"), nullable=False
    )
    speaker_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("speakers.id"), nullable=False
    )
    reply_to_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("utterances.id"), nullable=True
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    status: Mapped[str] = mapped_column(
        String(16), default=UTTERANCE_STATUS_RECEIVED, nullable=False
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
