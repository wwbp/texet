"""A missed Sunday must not cost the cohort a week of memory.

The summary job used to fire once, Sunday 00:00 UTC, from an in-process
scheduler with no persistent jobstore. If no instance was alive at that instant
— a deploy, a restart, an instance rotation — the job did not fire late, it
simply never fired, and the next run summarised the *following* week. The
skipped week was lost for every participant, silently.

Running it often instead makes it self-healing, which is only affordable if
already-summarised participants are skipped rather than regenerated.
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
from app.response.utils import week_start_utc
from app.summary.service import (
    SUMMARY_MODEL_ID,
    SUMMARY_PROVIDER,
    run_weekly_summaries,
)


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


def _previous_week_start() -> datetime.date:
    now = datetime.datetime.now(datetime.UTC)
    return week_start_utc(now) - datetime.timedelta(days=7)


async def _seed_last_week_activity(session: AsyncSession, user_id: str) -> None:
    """One received message inside the previous UTC week."""
    mid_prev_week = datetime.datetime.combine(
        _previous_week_start() + datetime.timedelta(days=3),
        datetime.time(12),
        tzinfo=datetime.UTC,
    )
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
        utt.timestamp = mid_prev_week


@pytest.fixture()
def counting_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    generated: list[str] = []

    async def _fake_generate_reply(
        _history: list[object],
        query: str,
        _prompt: str,
        *,
        provider: str,
        model_id: str,
    ) -> str:
        assert (provider, model_id) == (SUMMARY_PROVIDER, SUMMARY_MODEL_ID)
        generated.append(query)
        return f"summary #{len(generated)}"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    return generated


@pytest.mark.asyncio
async def test_generates_a_summary_for_an_active_participant(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    await _seed_last_week_activity(async_session, "u-catchup-new")

    await run_weekly_summaries(_sessionmaker_from(async_session))

    assert len(counting_llm) == 1
    stored = await get_weekly_summary(async_session, "u-catchup-new", _previous_week_start())
    assert stored == "summary #1"


@pytest.mark.asyncio
async def test_second_pass_regenerates_nothing(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    """Re-running hourly must be nearly free, or catch-up costs 24x per week."""
    await _seed_last_week_activity(async_session, "u-catchup-twice")

    await run_weekly_summaries(_sessionmaker_from(async_session))
    await run_weekly_summaries(_sessionmaker_from(async_session))
    await run_weekly_summaries(_sessionmaker_from(async_session))

    assert len(counting_llm) == 1, "already-summarised participant was regenerated"


@pytest.mark.asyncio
async def test_catches_up_a_participant_missed_by_an_earlier_run(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    """The whole point: a partial or skipped run is repaired by the next pass."""
    await _seed_last_week_activity(async_session, "u-catchup-done")
    await _seed_last_week_activity(async_session, "u-catchup-pending")

    # Simulate a run that completed one participant then died.
    async with async_session.begin():
        await upsert_weekly_summary(
            async_session, "u-catchup-done", _previous_week_start(), "already summarised"
        )

    await run_weekly_summaries(_sessionmaker_from(async_session))

    assert len(counting_llm) == 1, "should have summarised only the missed participant"
    assert (
        await get_weekly_summary(async_session, "u-catchup-done", _previous_week_start())
        == "already summarised"
    )
    pending = await get_weekly_summary(async_session, "u-catchup-pending", _previous_week_start())
    assert pending is not None and pending.startswith("summary #")


@pytest.mark.asyncio
async def test_no_activity_means_no_work(
    async_session: AsyncSession, counting_llm: list[str]
) -> None:
    await run_weekly_summaries(_sessionmaker_from(async_session))
    assert counting_llm == []


@pytest.mark.asyncio
async def test_one_failing_participant_does_not_block_the_rest(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_last_week_activity(async_session, "u-catchup-boom")
    await _seed_last_week_activity(async_session, "u-catchup-fine")

    async def _selective(_history: list[object], query: str, _prompt: str, **_model: str) -> str:
        if "u-catchup-boom" in query:
            raise RuntimeError("context window exceeded")
        return "ok summary"

    monkeypatch.setattr(response_service, "_generate_reply", _selective)

    await run_weekly_summaries(_sessionmaker_from(async_session))

    assert (
        await get_weekly_summary(async_session, "u-catchup-fine", _previous_week_start())
        == "ok summary"
    )
    assert await get_weekly_summary(async_session, "u-catchup-boom", _previous_week_start()) is None


@pytest.mark.asyncio
async def test_a_failed_participant_is_retried_next_pass(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping is keyed on a stored summary, so failures stay eligible."""
    await _seed_last_week_activity(async_session, "u-catchup-retry")

    attempts = {"n": 0}

    async def _fail_once(_history: list[object], _query: str, _prompt: str, **_model: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return "recovered summary"

    monkeypatch.setattr(response_service, "_generate_reply", _fail_once)

    await run_weekly_summaries(_sessionmaker_from(async_session))
    assert (
        await get_weekly_summary(async_session, "u-catchup-retry", _previous_week_start()) is None
    )

    await run_weekly_summaries(_sessionmaker_from(async_session))
    assert (
        await get_weekly_summary(async_session, "u-catchup-retry", _previous_week_start())
        == "recovered summary"
    )
