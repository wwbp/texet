"""Transient reply errors must retry instead of ghosting the participant.

An exception used to be terminal: the reply went straight to 'failed' and
`reclaim_stale` never looked at it again, because reclaim only rescues rows
stuck in 'processing'. That inverted the retry policy — a killed worker was
retried, but an LLM 429 was not.

The fix leaves the claim in place so the existing visibility timeout requeues
it, and only fails the reply once its attempts are spent.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_PROCESSING,
    UTTERANCE_STATUS_QUEUED,
)
from app.models.response import Utterance
from app.queue import reclaim_stale
from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


async def _queued_reply(session: AsyncSession, user_id: str, attempts: int) -> tuple[str, str]:
    """Create a reply already claimed by a worker. Returns (bot_id, user_id)."""
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, user_id)
        conversation = await get_or_create_conversation(session, speaker.id)
        user_utt = await create_utterance(session, conversation.id, speaker.id, "hello")
        bot_utt = await create_queued_utterance(
            session, conversation.id, bot.id, reply_to_id=user_utt.id
        )
        # Mirror what claim_next_reply does before handing work to the worker.
        bot_utt.status = UTTERANCE_STATUS_PROCESSING
        bot_utt.claimed_at = datetime.datetime.now(datetime.UTC)
        bot_utt.attempts = attempts
        ids = (bot_utt.id, user_utt.id)
    return ids


def _explode(monkeypatch: pytest.MonkeyPatch, message: str = "provider 429") -> None:
    async def _raise(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError(message)

    async def _allow(*_args: object, **_kwargs: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    monkeypatch.setattr(response_service, "_moderate_message", _allow)
    monkeypatch.setattr(response_service, "_moderate_text", _allow)
    monkeypatch.setattr(response_service, "_generate_reply", _raise)


@pytest.mark.asyncio
async def test_transient_error_keeps_the_claim_for_reclaim(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _explode(monkeypatch)
    bot_id, user_utt_id = await _queued_reply(async_session, "u-retry-1", attempts=1)

    await response_service._run_deferred_reply(
        "u-retry-1", user_utt_id, bot_id, _sessionmaker_from(async_session)
    )

    async_session.expire_all()
    reloaded = await async_session.get(Utterance, bot_id)
    assert reloaded is not None
    assert reloaded.status == UTTERANCE_STATUS_PROCESSING, "reply was failed instead of retried"
    assert reloaded.error is not None
    assert "provider 429" in reloaded.error


@pytest.mark.asyncio
async def test_expired_claim_is_requeued_after_a_transient_error(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry actually happens: reclaim_stale returns the reply to the queue."""
    _explode(monkeypatch)
    bot_id, user_utt_id = await _queued_reply(async_session, "u-retry-2", attempts=1)

    await response_service._run_deferred_reply(
        "u-retry-2", user_utt_id, bot_id, _sessionmaker_from(async_session)
    )

    async_session.expire_all()
    stale = await async_session.get(Utterance, bot_id)
    assert stale is not None
    stale.claimed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)
    await async_session.commit()

    requeued = await reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)
    assert requeued == 1

    async_session.expire_all()
    reloaded = await async_session.get(Utterance, bot_id)
    assert reloaded is not None
    assert reloaded.status == UTTERANCE_STATUS_QUEUED


@pytest.mark.asyncio
async def test_last_attempt_fails_with_the_real_error(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _explode(monkeypatch, "bedrock throttled")
    bot_id, user_utt_id = await _queued_reply(async_session, "u-retry-3", attempts=3)

    await response_service._run_deferred_reply(
        "u-retry-3", user_utt_id, bot_id, _sessionmaker_from(async_session)
    )

    async_session.expire_all()
    reloaded = await async_session.get(Utterance, bot_id)
    assert reloaded is not None
    assert reloaded.status == UTTERANCE_STATUS_FAILED
    assert reloaded.error is not None
    assert "bedrock throttled" in reloaded.error, "generic message replaced the real cause"
    assert reloaded.claimed_at is None


@pytest.mark.asyncio
async def test_reclaim_preserves_a_recorded_error_when_it_gives_up(
    async_session: AsyncSession,
) -> None:
    """A worker that died leaves no error; one that raised must keep its cause."""
    bot_id, _ = await _queued_reply(async_session, "u-retry-4", attempts=3)

    async with async_session.begin():
        stale = await async_session.get(Utterance, bot_id)
        assert stale is not None
        stale.error = "openai timeout"
        stale.claimed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)

    await reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)

    async_session.expire_all()
    reloaded = await async_session.get(Utterance, bot_id)
    assert reloaded is not None
    assert reloaded.status == UTTERANCE_STATUS_FAILED
    assert reloaded.error == "openai timeout"


@pytest.mark.asyncio
async def test_reclaim_still_explains_a_silently_dead_worker(
    async_session: AsyncSession,
) -> None:
    bot_id, _ = await _queued_reply(async_session, "u-retry-5", attempts=3)

    async with async_session.begin():
        stale = await async_session.get(Utterance, bot_id)
        assert stale is not None
        stale.claimed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=600)

    await reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)

    async_session.expire_all()
    reloaded = await async_session.get(Utterance, bot_id)
    assert reloaded is not None
    assert reloaded.status == UTTERANCE_STATUS_FAILED
    assert reloaded.error is not None
    assert "stale claims" in reloaded.error
