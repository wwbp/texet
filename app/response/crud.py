from __future__ import annotations

import datetime
import hashlib
from typing import Any

from kani import ChatMessage  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
    UTTERANCE_STATUSES,
)
from app.models.response import (
    Conversation,
    DailyPrompt,
    InstructionTemplate,
    Speaker,
    SummarizationPrompt,
    SystemPrompt,
    Utterance,
    WeeklySummary,
)
from app.response.prompt import DEFAULT_INSTRUCTION_TEMPLATE
from app.response.utils import day_marker, extract_utc_offset

DEFAULT_SYSTEM_PROMPT = "you are a helpful assistant."

DEFAULT_SUMMARIZATION_PROMPT = (
    "You are summarizing a week of conversation between a user and a chatbot. "
    "Produce a concise 3-5 sentence summary of the key topics, decisions, and "
    "context that would be useful for continuing the conversation next week. "
    "Focus on what the user shared about themselves and what was discussed."
)


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
    return await get_or_create_speaker(session, bot_speaker_id(user_id), meta={"type": "bot"})


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


async def get_latest_system_prompt(session: AsyncSession) -> SystemPrompt | None:
    result = await session.execute(
        select(SystemPrompt).order_by(SystemPrompt.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_or_create_system_prompt(session: AsyncSession) -> str:
    result = await session.execute(
        select(SystemPrompt).order_by(SystemPrompt.created_at.desc()).limit(1)
    )
    prompt = result.scalar_one_or_none()
    if not prompt:
        return DEFAULT_SYSTEM_PROMPT

    value = prompt.prompt.strip()
    if not value:
        return DEFAULT_SYSTEM_PROMPT
    return value


async def get_summarization_prompt(session: AsyncSession) -> str:
    result = await session.execute(
        select(SummarizationPrompt).order_by(SummarizationPrompt.created_at.desc()).limit(1)
    )
    prompt = result.scalar_one_or_none()
    if not prompt:
        return DEFAULT_SUMMARIZATION_PROMPT

    value = prompt.prompt.strip()
    if not value:
        return DEFAULT_SUMMARIZATION_PROMPT
    return value


async def get_instruction_template(session: AsyncSession) -> str:
    result = await session.execute(
        select(InstructionTemplate).order_by(InstructionTemplate.created_at.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if not row:
        return DEFAULT_INSTRUCTION_TEMPLATE

    value = row.template.strip()
    if not value:
        return DEFAULT_INSTRUCTION_TEMPLATE
    return value


def _local_date(timestamp: datetime.datetime, offset: datetime.timedelta | None) -> datetime.date:
    tz = datetime.timezone(offset) if offset is not None else datetime.UTC
    return timestamp.astimezone(tz).date()


async def build_chat_history(
    session: AsyncSession,
    conversation_id: str,
    user_id: str,
    up_to_timestamp: datetime.datetime,
    exclude_utterance_id: str | None = None,
    since_timestamp: datetime.datetime | None = None,
    annotate_days: bool = False,
) -> list[ChatMessage]:
    conditions = [
        Utterance.conversation_id == conversation_id,
        Utterance.timestamp <= up_to_timestamp,
    ]
    if since_timestamp is not None:
        conditions.append(Utterance.timestamp >= since_timestamp)
    result = await session.execute(
        select(Utterance).where(*conditions).order_by(Utterance.timestamp)
    )
    utterances = result.scalars().all()

    bot_id = bot_speaker_id(user_id)
    # Fidelity rule: the history mirrors what was actually exchanged over SMS.
    # Bot messages count only once delivered (sent); moderated exchanges are
    # withheld on both sides.
    included: list[Utterance] = []
    for utterance in utterances:
        if exclude_utterance_id and utterance.id == exclude_utterance_id:
            continue
        if not utterance.text:
            continue
        if utterance.speaker_id == bot_id:
            if utterance.status != UTTERANCE_STATUS_SENT:
                continue
        elif utterance.status == UTTERANCE_STATUS_MODERATED:
            continue
        included.append(utterance)

    # Leading messages without an offset of their own use the first known
    # one, so the whole history shares the user's timezone where possible.
    offset = next(
        (o for o in (extract_utc_offset(u.meta) for u in included) if o is not None),
        None,
    )
    previous_date: datetime.date | None = None
    chat_history: list[ChatMessage] = []
    for utterance in included:
        text = utterance.text
        if annotate_days:
            offset = extract_utc_offset(utterance.meta) or offset
            local_date = _local_date(utterance.timestamp, offset)
            if local_date != previous_date:
                text = f"{day_marker(local_date)}\n{text}"
                previous_date = local_date
        if utterance.speaker_id == bot_id:
            chat_history.append(ChatMessage.assistant(text))
        else:
            chat_history.append(ChatMessage.user(text))
    return chat_history


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


async def get_daily_prompt(
    session: AsyncSession,
    day_number: int,
) -> DailyPrompt | None:
    result = await session.execute(select(DailyPrompt).where(DailyPrompt.day_number == day_number))
    return result.scalar_one_or_none()


async def get_weekly_summary(
    session: AsyncSession,
    user_id: str,
    week_start: datetime.date,
) -> str | None:
    result = await session.execute(
        select(WeeklySummary).where(
            WeeklySummary.user_id == user_id,
            WeeklySummary.week_start == week_start,
        )
    )
    row = result.scalar_one_or_none()
    return row.summary if row else None


async def upsert_weekly_summary(
    session: AsyncSession,
    user_id: str,
    week_start: datetime.date,
    summary: str,
) -> None:
    result = await session.execute(
        select(WeeklySummary).where(
            WeeklySummary.user_id == user_id,
            WeeklySummary.week_start == week_start,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.summary = summary
    else:
        session.add(WeeklySummary(user_id=user_id, week_start=week_start, summary=summary))
    await session.flush()
