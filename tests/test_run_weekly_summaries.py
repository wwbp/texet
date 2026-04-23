from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_RECEIVED, UTTERANCE_STATUS_SENT
from app.response import service as response_service
from app.response.crud import (
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    get_weekly_summary,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc
from app.summary import service as summary_service
from app.summary.service import run_weekly_summaries


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


def _prev_week() -> tuple[datetime.date, datetime.datetime]:
    """Return (prev_week_start_date, a mid-week datetime) computed from today."""
    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    prev_week_start = current_week_start - datetime.timedelta(days=7)
    mid_week_dt = datetime.datetime.combine(
        prev_week_start + datetime.timedelta(days=3),
        datetime.time(12, 0, 0),
        tzinfo=datetime.UTC,
    )
    return prev_week_start, mid_week_dt


@pytest.mark.asyncio
async def test_run_weekly_summaries_generates_for_active_users(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Users with RECEIVED utterances in the previous week each get a summary."""
    prev_week_start, mid_week_dt = _prev_week()

    async def _fake_generate_reply(_history: list, query: str, _prompt: str) -> str:
        return f"summary: {query[:40]}"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)

    async with async_session.begin():
        for uid in ("u-rws-aa", "u-rws-bb"):
            speaker = await get_or_create_speaker(async_session, uid, meta={"type": "user"})
            await get_or_create_bot_speaker(async_session, uid)
            conv = await get_or_create_conversation(async_session, speaker.id)
            utt = await create_utterance(
                async_session, conv.id, speaker.id, f"hello from {uid}",
                status=UTTERANCE_STATUS_RECEIVED,
            )
            utt.timestamp = mid_week_dt

    sessionmaker = _sessionmaker_from(async_session)
    await run_weekly_summaries(sessionmaker)

    for uid in ("u-rws-aa", "u-rws-bb"):
        result = await get_weekly_summary(async_session, uid, prev_week_start)
        assert result is not None, f"Expected summary for {uid}"
        assert "summary:" in result


@pytest.mark.asyncio
async def test_run_weekly_summaries_skips_users_without_received_utterances(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with no RECEIVED utterances in the prev week is not summarized."""
    prev_week_start, mid_week_dt = _prev_week()
    called = {"n": 0}

    async def _should_not_be_called(*_args: object, **_kwargs: object) -> str:
        called["n"] += 1
        return "unexpected"

    monkeypatch.setattr(response_service, "_generate_reply", _should_not_be_called)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-rws-skip", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-rws-skip")
        conv = await get_or_create_conversation(async_session, speaker.id)
        # Only a bot-sent utterance in prev week — user never sent anything
        utt = await create_utterance(
            async_session, conv.id, bot.id, "bot message only",
            status=UTTERANCE_STATUS_SENT,
        )
        utt.timestamp = mid_week_dt

    sessionmaker = _sessionmaker_from(async_session)
    await run_weekly_summaries(sessionmaker)

    assert called["n"] == 0
    assert await get_weekly_summary(async_session, "u-rws-skip", prev_week_start) is None


@pytest.mark.asyncio
async def test_run_weekly_summaries_isolates_per_user_exceptions(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure for one user does not prevent summaries for others."""
    prev_week_start, mid_week_dt = _prev_week()

    async def _patched_generate(
        session: AsyncSession, user_id: str, week_start: datetime.date
    ) -> None:
        if user_id == "u-rws-err":
            raise RuntimeError("simulated failure")
        await upsert_weekly_summary(session, user_id, week_start, "ok summary")
        await session.commit()

    monkeypatch.setattr(summary_service, "generate_user_weekly_summary", _patched_generate)

    async with async_session.begin():
        for uid in ("u-rws-err", "u-rws-ok"):
            speaker = await get_or_create_speaker(async_session, uid, meta={"type": "user"})
            conv = await get_or_create_conversation(async_session, speaker.id)
            utt = await create_utterance(
                async_session, conv.id, speaker.id, "hello",
                status=UTTERANCE_STATUS_RECEIVED,
            )
            utt.timestamp = mid_week_dt

    sessionmaker = _sessionmaker_from(async_session)
    await run_weekly_summaries(sessionmaker)  # must not raise

    assert await get_weekly_summary(async_session, "u-rws-ok", prev_week_start) == "ok summary"
    assert await get_weekly_summary(async_session, "u-rws-err", prev_week_start) is None
