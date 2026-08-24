"""Engagement per participant per pinged day.

A pinged day is one where the hub opened the conversation (an utterance marked
texet_hub_initial). Engaged means the participant sent at least one message on
that same calendar day, in their own timezone.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_SENT
from app.engagement.service import compute_engagement
from app.response.crud import (
    create_conversation,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_speaker,
)

_EST = "2026-04-14T09:00:00-05:00"


async def _setup(session: AsyncSession, user_id: str) -> tuple[str, str, str]:
    speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
    bot = await get_or_create_bot_speaker(session, user_id)
    conversation = await create_conversation(session, speaker.id)
    await session.commit()
    return speaker.id, bot.id, conversation.id


async def _ping(
    session: AsyncSession, conv: str, bot: str, when: datetime.datetime, **meta: object
) -> None:
    u = await create_utterance(
        session,
        conv,
        bot,
        "Good morning!",
        meta={"texet_hub_initial": True, **meta},
        status=UTTERANCE_STATUS_SENT,
    )
    u.timestamp = when


async def _reply(
    session: AsyncSession, conv: str, bot: str, when: datetime.datetime, tokens: dict | None = None
) -> None:
    meta: dict = {}
    if tokens:
        meta["texet_usage"] = tokens
    u = await create_utterance(
        session, conv, bot, "a reply", meta=meta, status=UTTERANCE_STATUS_SENT
    )
    u.timestamp = when


async def _says(
    session: AsyncSession, conv: str, who: str, when: datetime.datetime, text: str = "hi"
) -> None:
    u = await create_utterance(session, conv, who, text)
    u.timestamp = when


@pytest.mark.asyncio
async def test_pinged_day_with_a_reply_is_engaged(async_session: AsyncSession) -> None:
    speaker, bot, conv = await _setup(async_session, "u-eng-1")
    day = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, day)
    await _says(async_session, conv, speaker, day + datetime.timedelta(hours=1))
    await _says(async_session, conv, speaker, day + datetime.timedelta(hours=2))
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert [(r.participant_id, r.date, r.engaged, r.utterance_count) for r in rows] == [
        ("u-eng-1", datetime.date(2026, 4, 14), True, 2)
    ]


@pytest.mark.asyncio
async def test_pinged_day_with_no_reply_is_not_engaged(async_session: AsyncSession) -> None:
    _, bot, conv = await _setup(async_session, "u-eng-2")
    await _ping(
        async_session, conv, bot, datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    )
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert len(rows) == 1
    assert rows[0].engaged is False
    assert rows[0].utterance_count == 0


@pytest.mark.asyncio
async def test_a_day_with_no_ping_is_not_reported(async_session: AsyncSession) -> None:
    """No ping means nothing to respond to; that is not a missed day."""
    speaker, bot, conv = await _setup(async_session, "u-eng-3")
    pinged = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    unpinged = datetime.datetime(2026, 4, 15, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, pinged)
    await _says(async_session, conv, speaker, unpinged)
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert [r.date for r in rows] == [datetime.date(2026, 4, 14)]


@pytest.mark.asyncio
async def test_reply_after_midnight_utc_counts_against_the_local_day(
    async_session: AsyncSession,
) -> None:
    """The point of using local days: a 9pm EST reply is 02:00 UTC the next day.
    Counting it in UTC would mark the participant as having missed the day."""
    speaker, bot, conv = await _setup(async_session, "u-eng-tz")
    # 08:00 EST on Apr 14 == 13:00 UTC; the ping carries the offset.
    await _ping(
        async_session,
        conv,
        bot,
        datetime.datetime(2026, 4, 14, 13, 0, tzinfo=datetime.UTC),
        user_local_time=_EST,
    )
    # 21:00 EST on Apr 14 == 02:00 UTC on Apr 15.
    await _says(
        async_session, conv, speaker, datetime.datetime(2026, 4, 15, 2, 0, tzinfo=datetime.UTC)
    )
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert [(r.date, r.engaged, r.utterance_count) for r in rows] == [
        (datetime.date(2026, 4, 14), True, 1)
    ]


@pytest.mark.asyncio
async def test_token_counts_sum_over_the_day(async_session: AsyncSession) -> None:
    _, bot, conv = await _setup(async_session, "u-eng-tok")
    day = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, day)
    await _reply(async_session, conv, bot, day, {"prompt_tokens": 100, "completion_tokens": 20})
    await _reply(async_session, conv, bot, day, {"prompt_tokens": 200, "completion_tokens": 30})
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert rows[0].token_count == 350


@pytest.mark.asyncio
async def test_token_count_is_none_when_nothing_was_measured(
    async_session: AsyncSession,
) -> None:
    """Every utterance predating usage capture looks like this. Unknown must not
    render as zero, which would read as a free day."""
    _, bot, conv = await _setup(async_session, "u-eng-notok")
    day = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, day)
    await _reply(async_session, conv, bot, day, None)
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert rows[0].token_count is None


@pytest.mark.asyncio
async def test_partial_measurement_counts_only_what_was_measured(
    async_session: AsyncSession,
) -> None:
    _, bot, conv = await _setup(async_session, "u-eng-part")
    day = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, day)
    await _reply(async_session, conv, bot, day, None)
    await _reply(async_session, conv, bot, day, {"prompt_tokens": 10, "completion_tokens": 5})
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert rows[0].token_count == 15


@pytest.mark.asyncio
async def test_date_range_filters_rows(async_session: AsyncSession) -> None:
    _, bot, conv = await _setup(async_session, "u-eng-range")
    for d in (13, 14, 15):
        await _ping(
            async_session, conv, bot, datetime.datetime(2026, 4, d, 12, 0, tzinfo=datetime.UTC)
        )
    await async_session.commit()

    rows = await compute_engagement(
        async_session, start=datetime.date(2026, 4, 14), end=datetime.date(2026, 4, 14)
    )

    assert [r.date for r in rows] == [datetime.date(2026, 4, 14)]


@pytest.mark.asyncio
async def test_rows_are_sorted_by_participant_then_date(async_session: AsyncSession) -> None:
    _, bot_b, conv_b = await _setup(async_session, "u-eng-zz")
    _, bot_a, conv_a = await _setup(async_session, "u-eng-aa")
    await _ping(
        async_session, conv_b, bot_b, datetime.datetime(2026, 4, 15, 12, tzinfo=datetime.UTC)
    )
    await _ping(
        async_session, conv_a, bot_a, datetime.datetime(2026, 4, 16, 12, tzinfo=datetime.UTC)
    )
    await _ping(
        async_session, conv_a, bot_a, datetime.datetime(2026, 4, 14, 12, tzinfo=datetime.UTC)
    )
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert [(r.participant_id, r.date.day) for r in rows] == [
        ("u-eng-aa", 14),
        ("u-eng-aa", 16),
        ("u-eng-zz", 15),
    ]


@pytest.mark.asyncio
async def test_bot_messages_do_not_count_as_participant_utterances(
    async_session: AsyncSession,
) -> None:
    _, bot, conv = await _setup(async_session, "u-eng-bot")
    day = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _ping(async_session, conv, bot, day)
    await _reply(async_session, conv, bot, day)
    await async_session.commit()

    rows = await compute_engagement(async_session)

    assert rows[0].utterance_count == 0
    assert rows[0].engaged is False
