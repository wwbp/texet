from __future__ import annotations

import datetime
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUSES,
)
from app.models import Conversation, Speaker, Utterance


def bot_speaker_id(user_id: str) -> str:
    prefix = "bot:"
    if len(user_id) <= 128 - len(prefix):
        return f"{prefix}{user_id}"
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


async def get_or_create_speaker(
    session: AsyncSession,
    speaker_id: str,
    meta: dict[str, Any] | None = None,
) -> Speaker:
    speaker = await session.get(Speaker, speaker_id)
    if speaker:
        return speaker

    try:
        async with session.begin_nested():
            speaker = Speaker(id=speaker_id, meta=meta)
            session.add(speaker)
            await session.flush()
            return speaker
    except IntegrityError:
        pass

    speaker = await session.get(Speaker, speaker_id)
    if not speaker:
        raise RuntimeError("Failed to create or fetch speaker.")
    return speaker


async def get_or_create_bot_speaker(session: AsyncSession, user_id: str) -> Speaker:
    return await get_or_create_speaker(
        session, bot_speaker_id(user_id), meta={"type": "bot"}
    )


async def create_conversation(
    session: AsyncSession,
    owner_speaker_id: str,
    status: str = "open",
    meta: dict[str, Any] | None = None,
) -> Conversation:
    conversation = Conversation(
        owner_speaker_id=owner_speaker_id,
        status=status,
        meta=meta,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def get_or_create_conversation(
    session: AsyncSession,
    owner_speaker_id: str,
    status: str = "open",
    meta: dict[str, Any] | None = None,
) -> Conversation:
    result = await session.execute(
        select(Conversation).where(
            Conversation.owner_speaker_id == owner_speaker_id,
            Conversation.status == status,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation:
        return conversation

    try:
        async with session.begin_nested():
            conversation = Conversation(
                owner_speaker_id=owner_speaker_id,
                status=status,
                meta=meta,
            )
            session.add(conversation)
            await session.flush()
            return conversation
    except IntegrityError:
        pass

    result = await session.execute(
        select(Conversation).where(
            Conversation.owner_speaker_id == owner_speaker_id,
            Conversation.status == status,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise RuntimeError("Failed to create or fetch conversation.")
    return conversation


async def create_utterance(
    session: AsyncSession,
    conversation_id: str,
    speaker_id: str,
    text: str,
    reply_to_id: str | None = None,
    meta: dict[str, Any] | None = None,
    status: str = UTTERANCE_STATUS_RECEIVED,
    error: str | None = None,
) -> Utterance:
    if text is None:
        raise ValueError("Utterance text is required.")
    if status not in UTTERANCE_STATUSES:
        raise ValueError(f"Invalid utterance status: {status}")
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    conversation = await session.get(Conversation, conversation_id)
    if not conversation:
        raise ValueError("Conversation not found for utterance.")

    utterance = Utterance(
        conversation_id=conversation_id,
        speaker_id=speaker_id,
        text=text,
        reply_to_id=reply_to_id,
        meta=meta,
        timestamp=now,
        status=status,
        error=error,
    )
    session.add(utterance)
    conversation.last_activity_at = now

    await session.flush()
    return utterance


async def create_queued_utterance(
    session: AsyncSession,
    conversation_id: str,
    speaker_id: str,
    reply_to_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Utterance:
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    conversation = await session.get(Conversation, conversation_id)
    if not conversation:
        raise ValueError("Conversation not found for utterance.")

    utterance = Utterance(
        conversation_id=conversation_id,
        speaker_id=speaker_id,
        text=None,
        reply_to_id=reply_to_id,
        meta=meta,
        timestamp=now,
        status=UTTERANCE_STATUS_QUEUED,
        error=None,
    )
    session.add(utterance)
    conversation.last_activity_at = now

    await session.flush()
    return utterance
