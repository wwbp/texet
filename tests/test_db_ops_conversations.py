import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_ops import (
    create_conversation,
    create_utterance,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.models import Conversation, Utterance


@pytest.mark.asyncio
async def test_create_conversation(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(
        async_session, "user-1", meta={"type": "user"}
    )
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    fetched = await async_session.get(Conversation, conversation.id)
    assert fetched is not None
    assert fetched.owner_speaker_id == speaker.id
    assert fetched.status == "open"
    assert fetched.created_at is not None
    assert fetched.last_activity_at is not None


@pytest.mark.asyncio
async def test_get_or_create_conversation_reuses(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(
        async_session, "user-1", meta={"type": "user"}
    )
    first = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    second = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    assert second.id == first.id
    count = await async_session.execute(select(func.count()).select_from(Conversation))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_create_utterance_updates_activity(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(
        async_session, "user-1", meta={"type": "user"}
    )
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    initial_activity = conversation.last_activity_at
    first = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "hello",
        reply_to_id=None,
    )
    second = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "follow-up",
        reply_to_id=first.id,
    )
    await async_session.commit()

    fetched_second = await async_session.get(Utterance, second.id)
    assert fetched_second is not None
    assert fetched_second.reply_to_id == first.id

    refreshed = await async_session.get(Conversation, conversation.id)
    assert refreshed is not None
    assert refreshed.last_activity_at >= initial_activity
