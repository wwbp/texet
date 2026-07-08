from __future__ import annotations

import asyncio
import datetime

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app import queue
from app.config import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_PROCESSING,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_SENT,
)
from app.models.response import Utterance
from app.queue import NOTIFY_CHANNEL, notify_reply_queued
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


async def _seed_queued_replies(
    session: AsyncSession, user_id: str, texts: list[str]
) -> list[Utterance]:
    """Create a user with one queued bot reply per text, timestamps strictly increasing."""
    base = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=len(texts) + 1)
    queued: list[Utterance] = []
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, user_id)
        conversation = await get_or_create_conversation(session, speaker.id)
        for i, text in enumerate(texts):
            user_utt = await create_utterance(session, conversation.id, speaker.id, text)
            user_utt.timestamp = base + datetime.timedelta(minutes=i)
            bot_utt = await create_queued_utterance(
                session, conversation.id, bot.id, reply_to_id=user_utt.id
            )
            bot_utt.timestamp = base + datetime.timedelta(minutes=i, seconds=30)
            queued.append(bot_utt)
    return queued


@pytest.mark.asyncio
async def test_claim_returns_none_on_empty_queue(async_session: AsyncSession) -> None:
    assert await queue.claim_next_reply(async_session) is None


@pytest.mark.asyncio
async def test_claim_takes_oldest_queued_and_marks_processing(
    async_session: AsyncSession,
) -> None:
    first, second = await _seed_queued_replies(async_session, "u-q-oldest", ["one", "two"])

    claimed = await queue.claim_next_reply(async_session)

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status == UTTERANCE_STATUS_PROCESSING
    assert claimed.claimed_at is not None
    assert claimed.attempts == 1

    untouched = await async_session.get(Utterance, second.id, populate_existing=True)
    assert untouched is not None
    assert untouched.status == UTTERANCE_STATUS_QUEUED


@pytest.mark.asyncio
async def test_claim_skips_user_with_reply_in_processing(
    async_session: AsyncSession,
) -> None:
    await _seed_queued_replies(async_session, "u-q-busy", ["one", "two"])
    await _seed_queued_replies(async_session, "u-q-free", ["hello"])

    first_claim = await queue.claim_next_reply(async_session)
    assert first_claim is not None  # u-q-busy's oldest (earliest timestamp overall)

    second_claim = await queue.claim_next_reply(async_session)
    assert second_claim is not None
    assert second_claim.speaker_id == "bot:u-q-free"

    # Both users now have a reply in flight; nothing else is claimable.
    assert await queue.claim_next_reply(async_session) is None


@pytest.mark.asyncio
async def test_concurrent_claims_never_double_claim_a_user(
    async_session: AsyncSession,
) -> None:
    await _seed_queued_replies(async_session, "u-q-race", ["one", "two"])
    sessionmaker = _sessionmaker_from(async_session)

    async def _claim() -> str | None:
        async with sessionmaker() as session:
            claimed = await queue.claim_next_reply(session)
            return claimed.id if claimed else None

    results = await asyncio.gather(_claim(), _claim())
    assert len([r for r in results if r is not None]) == 1


@pytest.mark.asyncio
async def test_concurrent_claims_distribute_across_users(
    async_session: AsyncSession,
) -> None:
    await _seed_queued_replies(async_session, "u-q-par-a", ["a"])
    await _seed_queued_replies(async_session, "u-q-par-b", ["b"])
    sessionmaker = _sessionmaker_from(async_session)

    async def _claim() -> str | None:
        async with sessionmaker() as session:
            claimed = await queue.claim_next_reply(session)
            return claimed.speaker_id if claimed else None

    results = await asyncio.gather(_claim(), _claim())
    assert sorted(r for r in results if r) == ["bot:u-q-par-a", "bot:u-q-par-b"]


@pytest.mark.asyncio
async def test_reclaim_stale_requeues_expired_claim(async_session: AsyncSession) -> None:
    (bot_utt,) = await _seed_queued_replies(async_session, "u-q-stale", ["one"])
    claimed = await queue.claim_next_reply(async_session)
    assert claimed is not None

    row = await async_session.get(Utterance, bot_utt.id)
    assert row is not None
    row.claimed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
    await async_session.commit()

    requeued = await queue.reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)
    assert requeued == 1

    row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
    assert row is not None
    assert row.status == UTTERANCE_STATUS_QUEUED
    assert row.claimed_at is None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_reclaim_stale_fails_reply_out_of_attempts(async_session: AsyncSession) -> None:
    (bot_utt,) = await _seed_queued_replies(async_session, "u-q-poison", ["one"])
    async with async_session.begin():
        row = await async_session.get(Utterance, bot_utt.id)
        assert row is not None
        row.status = UTTERANCE_STATUS_PROCESSING
        row.claimed_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        row.attempts = 3

    requeued = await queue.reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)
    assert requeued == 0

    row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
    assert row is not None
    assert row.status == UTTERANCE_STATUS_FAILED
    assert row.error is not None


@pytest.mark.asyncio
async def test_reclaim_stale_leaves_fresh_claims_alone(async_session: AsyncSession) -> None:
    (bot_utt,) = await _seed_queued_replies(async_session, "u-q-fresh", ["one"])
    claimed = await queue.claim_next_reply(async_session)
    assert claimed is not None

    requeued = await queue.reclaim_stale(async_session, reclaim_seconds=300, max_attempts=3)
    assert requeued == 0

    row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
    assert row is not None
    assert row.status == UTTERANCE_STATUS_PROCESSING


@pytest.mark.asyncio
async def test_count_pending_replies_counts_queued_and_processing(
    async_session: AsyncSession,
) -> None:
    await _seed_queued_replies(async_session, "u-q-count", ["one", "two"])
    (other,) = await _seed_queued_replies(async_session, "u-q-count2", ["three"])

    async with async_session.begin():
        row = await async_session.get(Utterance, other.id)
        assert row is not None
        row.status = UTTERANCE_STATUS_PROCESSING
        speaker = await get_or_create_speaker(async_session, "u-q-count3", meta={"type": "user"})
        conversation = await get_or_create_conversation(async_session, speaker.id)
        sent = await create_utterance(async_session, conversation.id, speaker.id, "done")
        sent.status = UTTERANCE_STATUS_SENT

    assert await queue.count_pending_replies(async_session) == 3

@pytest.mark.asyncio
async def test_notify_reply_queued_is_received_by_a_listener(
    async_session: AsyncSession,
) -> None:
    """A LISTEN connection receives the NOTIFY the API issues on enqueue."""
    engine = _sessionmaker_from(async_session).kw["bind"]
    dsn = engine.url.render_as_string(hide_password=False).replace("+asyncpg", "", 1)

    received = asyncio.Event()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.add_listener(NOTIFY_CHANNEL, lambda *_a: received.set())
        async with async_session.begin():
            await notify_reply_queued(async_session)
        await asyncio.wait_for(received.wait(), timeout=5)
        assert received.is_set()
    finally:
        await conn.close()
