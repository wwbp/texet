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
    get_participant_utc_offset,
    get_summarization_prompt,
    upsert_weekly_summary,
)
from app.response.utils import week_bounds_utc, week_start_for

logger = logging.getLogger(__name__)

# The summariser's model is named here rather than inherited. It used to call
# _generate_reply with no provider or model, so summaries ran on that function's
# openai/gpt-4o-mini defaults while replies ran on the bedrock llama model from
# the latest system_prompts row. Nobody decided that split; it was default
# arguments showing through, and it meant an edit to those defaults would have
# moved every participant's summaries mid-study without this file being touched.
#
# This matches what replies run on today. It is deliberately a separate pin and
# not a read of system_prompts: a console change to the reply model should not
# silently re-summarise a study on something else. Moving summaries is a choice
# someone makes here, in a diff.
SUMMARY_PROVIDER = "bedrock"
SUMMARY_MODEL_ID = "us.meta.llama4-maverick-17b-instruct-v1:0"


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


def _week_bounds(
    week_start: datetime.date,
    offset: datetime.timedelta | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """UTC instants for a participant's local week. UTC when no offset is known."""
    return week_bounds_utc(week_start, offset)


async def active_user_ids(
    session: AsyncSession,
    week_start: datetime.date,
    offset: datetime.timedelta | None = None,
) -> list[str]:
    """Participants who sent at least one message during the week."""
    week_start_dt, week_end_dt = _week_bounds(week_start, offset)
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
    offset: datetime.timedelta | None = None,
) -> bool:
    """Summarise one participant's week. Returns False when there is nothing to summarise.

    The week is the participant's local week. Passing offset avoids a second
    lookup when the caller already resolved it; omitting it resolves it here.
    """
    if offset is None:
        offset = await get_participant_utc_offset(session, user_id)
    week_start_dt, week_end_dt = _week_bounds(week_start, offset)

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
    summary = await response_service._generate_reply(
        [],
        transcript,
        instruction,
        provider=SUMMARY_PROVIDER,
        model_id=SUMMARY_MODEL_ID,
    )
    await upsert_weekly_summary(session, user_id, week_start, summary)
    await session.commit()
    return True


async def _recently_active_user_ids(
    session: AsyncSession,
    since: datetime.datetime,
) -> list[str]:
    """Participants with a message since `since`, on any clock.

    Candidates are gathered on a deliberately wide UTC window and then judged
    on each participant's own week, because whose week has ended is not a
    question that can be asked of everyone at once.
    """
    result = await session.execute(
        select(Utterance.speaker_id)
        .where(
            Utterance.status == UTTERANCE_STATUS_RECEIVED,
            Utterance.timestamp >= since,
        )
        .distinct()
    )
    return list(result.scalars().all())


async def run_weekly_summaries(
    sessionmaker: async_sessionmaker[AsyncSession],
    now: datetime.datetime | None = None,
) -> None:
    """Summarise each participant's most recently completed local week.

    The week that has just ended is asked per participant rather than once for
    everyone: at 02:00 UTC on a Sunday a UTC-5 participant is still in Saturday
    evening, and summarising them then would both cut their week five hours
    short and read messages they had not sent yet. Whoever's local week has
    already rolled over gets summarised on this pass; the rest are picked up by
    a later one, which is why the job runs hourly.
    """
    now_utc = now or datetime.datetime.now(datetime.UTC)
    # Two weeks of slack so a participant whose local week ends after the UTC
    # one, or who was missed by an outage, is still a candidate.
    candidates_since = now_utc - datetime.timedelta(days=21)

    async with sessionmaker() as session:
        user_ids = await _recently_active_user_ids(session, candidates_since)
        offsets = {
            user_id: await get_participant_utc_offset(session, user_id) for user_id in user_ids
        }

        # (user_id, week_start) pairs already done. The job runs often so that a
        # Sunday missed entirely — no instance alive when the cron fired — is
        # repaired on the next pass instead of costing everyone a week of
        # memory. That is only affordable if repeat passes do no work.
        wanted = {
            user_id: week_start_for(now_utc, offsets[user_id]) - datetime.timedelta(days=7)
            for user_id in user_ids
        }
        if not wanted:
            return
        done = await session.execute(
            select(WeeklySummary.user_id, WeeklySummary.week_start).where(
                WeeklySummary.week_start.in_(set(wanted.values()))
            )
        )
        already_summarized = set(done.all())

    pending = [
        (user_id, week_start)
        for user_id, week_start in wanted.items()
        if (user_id, week_start) not in already_summarized
    ]
    if not pending:
        return

    logger.info(
        "Generating weekly summaries: %d pending across %d local weeks.",
        len(pending),
        len({week_start for _, week_start in pending}),
    )

    for user_id, week_start in pending:
        try:
            async with sessionmaker() as session:
                await generate_user_weekly_summary(
                    session, user_id, week_start, offsets.get(user_id)
                )
        except Exception:
            logger.exception(
                "Failed to generate weekly summary for user=%s week=%s", user_id, week_start
            )


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
