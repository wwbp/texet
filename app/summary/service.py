from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_MODERATED, UTTERANCE_STATUS_RECEIVED
from app.models.response import Utterance
from app.response import service as response_service
from app.response.crud import (
    bot_speaker_id,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc

logger = logging.getLogger(__name__)

SUMMARIZATION_PROMPT = (
    "You are summarizing a week of conversation between a user and a chatbot. "
    "Produce a concise 3-5 sentence summary of the key topics, decisions, and "
    "context that would be useful for continuing the conversation next week. "
    "Focus on what the user shared about themselves and what was discussed."
)


def build_week_transcript(utterances: list[Utterance], user_id: str) -> str:
    bot_id = bot_speaker_id(user_id)
    lines: list[str] = []
    for u in utterances:
        if u.status == UTTERANCE_STATUS_MODERATED:
            continue
        if not u.text:
            continue
        role = "bot" if u.speaker_id == bot_id else "user"
        lines.append(f"{role}: {u.text}")
    return "\n".join(lines)


async def generate_user_weekly_summary(
    session: AsyncSession,
    user_id: str,
    week_start: datetime.date,
) -> None:
    week_end = week_start + datetime.timedelta(days=7)
    week_start_dt = datetime.datetime.combine(week_start, datetime.time.min, tzinfo=datetime.UTC)
    week_end_dt = datetime.datetime.combine(week_end, datetime.time.min, tzinfo=datetime.UTC)

    result = await session.execute(
        select(Utterance)
        .where(
            Utterance.timestamp >= week_start_dt,
            Utterance.timestamp < week_end_dt,
            Utterance.speaker_id.in_([user_id, bot_speaker_id(user_id)]),
        )
        .order_by(Utterance.timestamp)
    )
    utterances = result.scalars().all()

    transcript = build_week_transcript(list(utterances), user_id)
    if not transcript.strip():
        return

    summary = await response_service._generate_reply([], transcript, SUMMARIZATION_PROMPT)
    await upsert_weekly_summary(session, user_id, week_start, summary)
    await session.commit()


async def run_weekly_summaries(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    prev_week_start = current_week_start - datetime.timedelta(days=7)
    week_start_dt = datetime.datetime.combine(
        prev_week_start, datetime.time.min, tzinfo=datetime.UTC
    )
    week_end_dt = datetime.datetime.combine(
        current_week_start, datetime.time.min, tzinfo=datetime.UTC
    )

    async with sessionmaker() as session:
        result = await session.execute(
            select(Utterance.speaker_id)
            .where(
                Utterance.status == UTTERANCE_STATUS_RECEIVED,
                Utterance.timestamp >= week_start_dt,
                Utterance.timestamp < week_end_dt,
            )
            .distinct()
        )
        user_ids = list(result.scalars().all())

    for user_id in user_ids:
        try:
            async with sessionmaker() as session:
                await generate_user_weekly_summary(session, user_id, prev_week_start)
        except Exception:
            logger.exception("Failed to generate weekly summary for user=%s", user_id)
