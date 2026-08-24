"""Weekly summaries run on each participant's own week.

The boundary used to be UTC for everyone. For a participant at UTC-5 that is
Saturday 19:00 local: the summary for their week was generated five hours
before that week had finished, and the messages they sent in those hours were
filed under the following week instead.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_RECEIVED
from app.models.response import WeeklySummary
from app.response import service as response_service
from app.response.crud import (
    create_utterance,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.summary.service import run_weekly_summaries

# 02:00 UTC on Sunday 23 Aug 2026 == 21:00 EST on Saturday 22 Aug.
SUNDAY_0200_UTC = datetime.datetime(2026, 8, 23, 2, 0, tzinfo=datetime.UTC)
# 06:00 UTC the same day == 01:00 EST on Sunday, past the local boundary.
SUNDAY_0600_UTC = datetime.datetime(2026, 8, 23, 6, 0, tzinfo=datetime.UTC)

WEEK_OF_AUG_16 = datetime.date(2026, 8, 16)
EST_LOCAL_TIME = "2026-08-19T09:00:00-05:00"


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def _fake(_history: list[object], query: str, _prompt: str, **_kw: object) -> str:
        seen.append(query)
        return "a summary"

    monkeypatch.setattr(response_service, "_generate_reply", _fake)
    return seen


async def _seed(
    session: AsyncSession,
    user_id: str,
    when: datetime.datetime,
    *,
    local_time: str | None,
    text: str = "hello",
) -> None:
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        conversation = await get_or_create_conversation(session, speaker.id)
        utt = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            text,
            meta={"user_local_time": local_time} if local_time else None,
            status=UTTERANCE_STATUS_RECEIVED,
        )
        utt.timestamp = when


async def _summaries(session: AsyncSession, user_id: str) -> list[datetime.date]:
    result = await session.execute(
        select(WeeklySummary.week_start).where(WeeklySummary.user_id == user_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_local_week_not_yet_over_is_not_summarised(
    async_session: AsyncSession, stub_llm: list[str]
) -> None:
    """At 02:00 UTC Sunday the UTC week has rolled over but a UTC-5 participant
    is still in Saturday evening. Summarising now would cut their week short."""
    await _seed(
        async_session,
        "u-tz-early",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time=EST_LOCAL_TIME,
    )

    await run_weekly_summaries(_sessionmaker_from(async_session), now=SUNDAY_0200_UTC)

    assert WEEK_OF_AUG_16 not in await _summaries(async_session, "u-tz-early")


@pytest.mark.asyncio
async def test_same_participant_is_summarised_once_their_week_ends(
    async_session: AsyncSession, stub_llm: list[str]
) -> None:
    await _seed(
        async_session,
        "u-tz-late",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time=EST_LOCAL_TIME,
    )

    await run_weekly_summaries(_sessionmaker_from(async_session), now=SUNDAY_0600_UTC)

    assert WEEK_OF_AUG_16 in await _summaries(async_session, "u-tz-late")


@pytest.mark.asyncio
async def test_participant_without_a_local_time_falls_back_to_utc(
    async_session: AsyncSession, stub_llm: list[str]
) -> None:
    """No offset recorded: the UTC week is the week, so 02:00 Sunday is late
    enough. This is the behaviour every participant used to get."""
    await _seed(
        async_session,
        "u-tz-none",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time=None,
    )

    await run_weekly_summaries(_sessionmaker_from(async_session), now=SUNDAY_0200_UTC)

    assert WEEK_OF_AUG_16 in await _summaries(async_session, "u-tz-none")


@pytest.mark.asyncio
async def test_saturday_evening_local_messages_reach_the_right_week(
    async_session: AsyncSession, stub_llm: list[str]
) -> None:
    """The substantive win. A 21:00 EST Saturday message is 02:00 UTC Sunday.
    On UTC weeks it landed in the following week and the summary that should
    have contained it had already been written."""
    await _seed(
        async_session,
        "u-tz-sat",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time=EST_LOCAL_TIME,
        text="midweek message",
    )
    await _seed(
        async_session,
        "u-tz-sat",
        SUNDAY_0200_UTC,
        local_time=EST_LOCAL_TIME,
        text="saturday night message",
    )

    await run_weekly_summaries(_sessionmaker_from(async_session), now=SUNDAY_0600_UTC)

    assert WEEK_OF_AUG_16 in await _summaries(async_session, "u-tz-sat")
    transcript = stub_llm[0]
    assert "midweek message" in transcript
    assert "saturday night message" in transcript


@pytest.mark.asyncio
async def test_participants_on_different_clocks_are_judged_separately(
    async_session: AsyncSession, stub_llm: list[str]
) -> None:
    """One pass, two verdicts: the Tokyo participant's week ended hours ago, the
    New York participant's has not. Asking the question once for everyone is
    what the old job did."""
    await _seed(
        async_session,
        "u-tz-nyc",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time=EST_LOCAL_TIME,
    )
    await _seed(
        async_session,
        "u-tz-tokyo",
        datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.UTC),
        local_time="2026-08-19T23:00:00+09:00",
    )

    await run_weekly_summaries(_sessionmaker_from(async_session), now=SUNDAY_0200_UTC)

    assert WEEK_OF_AUG_16 not in await _summaries(async_session, "u-tz-nyc")
    assert WEEK_OF_AUG_16 in await _summaries(async_session, "u-tz-tokyo")
