from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import DEFAULT_TIMEZONE, UTTERANCE_STATUS_RECEIVED, UTTERANCE_STATUSES_SQL
from app.models.base import Base


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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_speaker_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("speakers.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="open")
    last_activity_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(DEFAULT_TIMEZONE),
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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id"), nullable=False
    )
    speaker_id: Mapped[str] = mapped_column(String(128), ForeignKey("speakers.id"), nullable=False)
    reply_to_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("utterances.id"), nullable=True
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(DEFAULT_TIMEZONE),
    )
    status: Mapped[str] = mapped_column(
        String(16), default=UTTERANCE_STATUS_RECEIVED, nullable=False
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class SystemPrompt(Base):
    __tablename__ = "system_prompts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="openai", server_default="openai"
    )
    model_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="gpt-4o-mini", server_default="gpt-4o-mini"
    )


class WeeklySummary(Base):
    __tablename__ = "weekly_summaries"
    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_weekly_summaries_user_week"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String(128), ForeignKey("speakers.id"), nullable=False)
    week_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
