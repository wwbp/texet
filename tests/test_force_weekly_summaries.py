"""Admin-triggered regeneration of weekly summaries.

The hourly job skips participants that already have a summary for the week —
that is what makes catch-up passes affordable. It also makes the job useless
for testing: seed a week of conversation, run it once, and every later run is
a no-op. The console button needs a path that ignores the skip and rebuilds
the week from whatever is currently in the database.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_RECEIVED
from app.response import service as response_service
from app.response.crud import (
    create_utterance,
    get_or_create_conversation,
    get_or_create_speaker,
    get_weekly_summary,
    upsert_weekly_summary,
)
from app.summary.service import force_weekly_summaries

_WEEK_START = datetime.date(2026, 4, 12)  # a Sunday
_WEEK_MID_DT = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
_OTHER_WEEK_DT = datetime.datetime(2026, 4, 7, 12, 0, tzinfo=datetime.UTC)


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_message(
    session: AsyncSession, user_id: str, at: datetime.datetime = _WEEK_MID_DT
) -> None:
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        conversation = await get_or_create_conversation(session, speaker.id)
        utt = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            f"message from {user_id}",
            status=UTTERANCE_STATUS_RECEIVED,
        )
        utt.timestamp = at


@pytest.fixture()
def counting_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    generated: list[str] = []

    async def _fake_generate_reply(_history: list[object], query: str, _prompt: str) -> str:
        generated.append(query)
        return f"forced summary #{len(generated)}"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    return generated


@pytest.mark.asyncio
async def test_generates_for_every_active_participant(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    await _seed_message(async_session, "u-force-a")
    await _seed_message(async_session, "u-force-b")

    result = await force_weekly_summaries(_sessionmaker_from(async_session), _WEEK_START)

    assert (result.users, result.generated, result.failed) == (2, 2, 0)
    assert await get_weekly_summary(async_session, "u-force-a", _WEEK_START) is not None
    assert await get_weekly_summary(async_session, "u-force-b", _WEEK_START) is not None


@pytest.mark.asyncio
async def test_overwrites_an_existing_summary(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    """The whole point of the button: unlike the cron, it does not skip."""
    await _seed_message(async_session, "u-force-again")
    async with async_session.begin():
        await upsert_weekly_summary(async_session, "u-force-again", _WEEK_START, "stale summary")

    result = await force_weekly_summaries(_sessionmaker_from(async_session), _WEEK_START)

    assert result.generated == 1
    async_session.expire_all()
    assert (
        await get_weekly_summary(async_session, "u-force-again", _WEEK_START) == "forced summary #1"
    )


@pytest.mark.asyncio
async def test_only_covers_the_requested_week(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    await _seed_message(async_session, "u-force-in", _WEEK_MID_DT)
    await _seed_message(async_session, "u-force-out", _OTHER_WEEK_DT)

    result = await force_weekly_summaries(_sessionmaker_from(async_session), _WEEK_START)

    assert result.users == 1
    assert await get_weekly_summary(async_session, "u-force-out", _WEEK_START) is None


@pytest.mark.asyncio
async def test_no_activity_reports_zero(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    result = await force_weekly_summaries(_sessionmaker_from(async_session), _WEEK_START)

    assert (result.users, result.generated, result.failed) == (0, 0, 0)
    assert counting_llm == []


@pytest.mark.asyncio
async def test_one_failure_does_not_block_the_rest(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_message(async_session, "u-force-boom")
    await _seed_message(async_session, "u-force-fine")

    async def _selective(_history: list[object], query: str, _prompt: str) -> str:
        if "u-force-boom" in query:
            raise RuntimeError("context window exceeded")
        return "ok summary"

    monkeypatch.setattr(response_service, "_generate_reply", _selective)

    result = await force_weekly_summaries(_sessionmaker_from(async_session), _WEEK_START)

    assert (result.users, result.generated, result.failed) == (2, 1, 1)
    assert await get_weekly_summary(async_session, "u-force-fine", _WEEK_START) == "ok summary"
    assert await get_weekly_summary(async_session, "u-force-boom", _WEEK_START) is None
