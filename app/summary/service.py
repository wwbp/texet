from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_MODERATED, UTTERANCE_STATUS_RECEIVED
from app.models.response import Utterance, WeeklySummary
from app.response import service as response_service
from app.response.crud import (
    bot_speaker_id,
    get_summarization_prompt,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForceSummaryResult:
    users: int
    generated: int
    failed: int


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


def _week_bounds(week_start: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    week_end = week_start + datetime.timedelta(days=7)
    return (
        datetime.datetime.combine(week_start, datetime.time.min, tzinfo=datetime.UTC),
        datetime.datetime.combine(week_end, datetime.time.min, tzinfo=datetime.UTC),
    )


async def active_user_ids(session: AsyncSession, week_start: datetime.date) -> list[str]:
    """Participants who sent at least one message during the week."""
    week_start_dt, week_end_dt = _week_bounds(week_start)
    result = await session.execute(
        select(Utterance.speaker_id)
        .where(
            Utterance.status == UTTERANCE_STATUS_RECEIVED,
            Utterance.timestamp >= week_start_dt,
            Utterance.timestamp < week_end_dt,
        )
        .distinct()
    )
    return list(result.scalars().all())


async def generate_user_weekly_summary(
    session: AsyncSession,
    user_id: str,
    week_start: datetime.date,
) -> bool:
    """Summarise one participant's week. Returns False when there is nothing to summarise."""
    week_start_dt, week_end_dt = _week_bounds(week_start)

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
        return False

    instruction = await get_summarization_prompt(session)
    summary = await response_service._generate_reply([], transcript, instruction)
    await upsert_weekly_summary(session, user_id, week_start, summary)
    await session.commit()
    return True


async def run_weekly_summaries(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    prev_week_start = current_week_start - datetime.timedelta(days=7)

    async with sessionmaker() as session:
        user_ids = await active_user_ids(session, prev_week_start)

        # Skip participants already summarised for this week. The job runs
        # often so that a Sunday missed entirely — no instance alive when the
        # cron fired — is repaired on the next pass instead of costing everyone
        # a week of memory. That is only affordable if repeat passes do no work.
        done = await session.execute(
            select(WeeklySummary.user_id).where(WeeklySummary.week_start == prev_week_start)
        )
        already_summarized = set(done.scalars().all())

    pending = [user_id for user_id in user_ids if user_id not in already_summarized]
    if not pending:
        return

    logger.info(
        "Generating weekly summaries for week=%s: %d pending, %d already done.",
        prev_week_start,
        len(pending),
        len(already_summarized),
    )

    for user_id in pending:
        try:
            async with sessionmaker() as session:
                await generate_user_weekly_summary(session, user_id, prev_week_start)
        except Exception:
            logger.exception("Failed to generate weekly summary for user=%s", user_id)


async def force_weekly_summaries(
    sessionmaker: async_sessionmaker[AsyncSession],
    week_start: datetime.date,
) -> ForceSummaryResult:
    """Regenerate summaries for every participant active in the given week.

    Unlike the scheduled job this ignores existing summaries, which is the only
    way to exercise the summarizer against data already in the database — the
    cron would see the week as done and do nothing.
    """
    async with sessionmaker() as session:
        user_ids = await active_user_ids(session, week_start)

    logger.info("Forcing weekly summaries for week=%s: %d participants.", week_start, len(user_ids))

    generated = 0
    failed = 0
    for user_id in user_ids:
        try:
            async with sessionmaker() as session:
                if await generate_user_weekly_summary(session, user_id, week_start):
                    generated += 1
        except Exception:
            failed += 1
            logger.exception("Failed to force weekly summary for user=%s", user_id)

    return ForceSummaryResult(users=len(user_ids), generated=generated, failed=failed)
