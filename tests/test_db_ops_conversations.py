import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
)
from app.models.response import Conversation, SystemPrompt, Utterance
from app.response.crud import (
    DEFAULT_SYSTEM_PROMPT,
    build_chat_history,
    create_conversation,
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    get_or_create_system_prompt,
)


@pytest.mark.asyncio
async def test_create_conversation(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
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
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    first = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    second = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    assert second.id == first.id
    count = await async_session.execute(select(func.count()).select_from(Conversation))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_different_day_identifiers_create_separate_conversations(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-day-scope", meta={"type": "user"})

    conv_day1 = await get_or_create_conversation(async_session, speaker.id, day_identifier=1)
    await async_session.commit()

    conv_day2 = await get_or_create_conversation(async_session, speaker.id, day_identifier=2)
    await async_session.commit()

    assert conv_day1.id != conv_day2.id
    assert conv_day1.day_identifier == 1
    assert conv_day2.day_identifier == 2

    count = await async_session.execute(select(func.count()).select_from(Conversation))
    assert count.scalar_one() == 2


@pytest.mark.asyncio
async def test_same_day_identifier_reuses_conversation(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(async_session, "user-day-reuse", meta={"type": "user"})

    first = await get_or_create_conversation(async_session, speaker.id, day_identifier=5)
    await async_session.commit()

    second = await get_or_create_conversation(async_session, speaker.id, day_identifier=5)
    await async_session.commit()

    assert second.id == first.id
    count = await async_session.execute(select(func.count()).select_from(Conversation))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_no_day_conversation_is_separate_from_day_conversations(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-day-null", meta={"type": "user"})

    conv_no_day = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    conv_day1 = await get_or_create_conversation(async_session, speaker.id, day_identifier=1)
    await async_session.commit()

    assert conv_no_day.id != conv_day1.id
    assert conv_no_day.day_identifier is None
    assert conv_day1.day_identifier == 1

    # No-day conversation is still reused when called without day_identifier
    again = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()
    assert again.id == conv_no_day.id


@pytest.mark.asyncio
async def test_create_utterance_updates_activity(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
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

    assert first.status == UTTERANCE_STATUS_RECEIVED
    assert first.error is None
    assert second.status == UTTERANCE_STATUS_RECEIVED
    assert second.error is None


@pytest.mark.asyncio
async def test_create_utterance_rejects_none_text(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    with pytest.raises(ValueError, match="Utterance text is required."):
        await create_utterance(  # type: ignore[arg-type]
            async_session,
            conversation.id,
            speaker.id,
            None,
        )


@pytest.mark.asyncio
async def test_create_utterance_rejects_invalid_status(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    with pytest.raises(ValueError, match="Invalid utterance status"):
        await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hello",
            status="bogus",
        )


@pytest.mark.asyncio
async def test_create_queued_utterance(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    queued = await create_queued_utterance(
        async_session,
        conversation.id,
        speaker.id,
        reply_to_id=None,
    )
    await async_session.commit()

    fetched = await async_session.get(Utterance, queued.id)
    assert fetched is not None
    assert fetched.status == UTTERANCE_STATUS_QUEUED
    assert fetched.text is None
    assert fetched.error is None


@pytest.mark.asyncio
async def test_create_queued_utterance_requires_conversation(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    await async_session.commit()

    with pytest.raises(ValueError, match="Conversation not found"):
        await create_queued_utterance(
            async_session,
            "missing",
            speaker.id,
        )


@pytest.mark.asyncio
async def test_get_or_create_system_prompt_sets_default(
    async_session: AsyncSession,
) -> None:
    prompt = await get_or_create_system_prompt(async_session)
    assert prompt == DEFAULT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_get_or_create_system_prompt_uses_latest_created(
    async_session: AsyncSession,
) -> None:
    base = datetime.datetime(2026, 1, 1, tzinfo=DEFAULT_TIMEZONE)
    first = SystemPrompt(prompt="first", created_at=base)
    second = SystemPrompt(prompt="second", created_at=base + datetime.timedelta(seconds=1))
    async_session.add_all([first, second])
    await async_session.commit()

    prompt = await get_or_create_system_prompt(async_session)
    assert prompt == "second"

    async_session.add(SystemPrompt(prompt="third", created_at=base + datetime.timedelta(seconds=2)))
    await async_session.commit()

    latest = await get_or_create_system_prompt(async_session)
    assert latest == "third"


@pytest.mark.asyncio
async def test_get_or_create_system_prompt_propagates_query_error(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_: object, **__: object) -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(async_session, "execute", _boom)

    with pytest.raises(RuntimeError, match="db down"):
        await get_or_create_system_prompt(async_session)


@pytest.mark.asyncio
async def test_build_chat_history_orders_roles_and_excludes(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-1")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

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
        bot.id,
        "hi there",
        reply_to_id=first.id,
    )
    third = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "next",
        reply_to_id=second.id,
    )
    base = datetime.datetime(2026, 1, 1, tzinfo=DEFAULT_TIMEZONE)
    first.timestamp = base
    second.timestamp = base + datetime.timedelta(seconds=1)
    third.timestamp = base + datetime.timedelta(seconds=2)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-1",
        up_to_timestamp=third.timestamp,
        exclude_utterance_id=third.id,
    )

    assert len(history) == 2
    assert history[0].role.value == "user"
    assert history[0].content == "hello"
    assert history[1].role.value == "assistant"
    assert history[1].content == "hi there"


@pytest.mark.asyncio
async def test_build_chat_history_filters_by_conversation(
    async_session: AsyncSession,
) -> None:
    speaker_one = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
    speaker_two = await get_or_create_speaker(async_session, "user-2", meta={"type": "user"})
    bot_one = await get_or_create_bot_speaker(async_session, "user-1")
    bot_two = await get_or_create_bot_speaker(async_session, "user-2")
    conversation_one = await create_conversation(async_session, speaker_one.id)
    conversation_two = await create_conversation(async_session, speaker_two.id)
    await async_session.commit()

    base = datetime.datetime(2026, 1, 2, tzinfo=DEFAULT_TIMEZONE)
    one_user = await create_utterance(
        async_session,
        conversation_one.id,
        speaker_one.id,
        "one-hello",
    )
    one_bot = await create_utterance(
        async_session,
        conversation_one.id,
        bot_one.id,
        "one-reply",
        reply_to_id=one_user.id,
    )
    two_user = await create_utterance(
        async_session,
        conversation_two.id,
        speaker_two.id,
        "two-hello",
    )
    two_bot = await create_utterance(
        async_session,
        conversation_two.id,
        bot_two.id,
        "two-reply",
        reply_to_id=two_user.id,
    )

    one_user.timestamp = base
    one_bot.timestamp = base + datetime.timedelta(seconds=1)
    two_user.timestamp = base + datetime.timedelta(seconds=2)
    two_bot.timestamp = base + datetime.timedelta(seconds=3)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation_one.id,
        user_id="user-1",
        up_to_timestamp=two_bot.timestamp,
    )

    assert [(msg.role.value, msg.content) for msg in history] == [
        ("user", "one-hello"),
        ("assistant", "one-reply"),
    ]


@pytest.mark.asyncio
async def test_build_chat_history_empty_on_first_message(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-3", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-3")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    user_utterance = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "hello",
    )
    await create_queued_utterance(
        async_session,
        conversation.id,
        bot.id,
        reply_to_id=user_utterance.id,
    )
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-3",
        up_to_timestamp=user_utterance.timestamp,
        exclude_utterance_id=user_utterance.id,
    )

    assert history == []


@pytest.mark.asyncio
async def test_build_chat_history_skips_moderated_utterances(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-4", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-4")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    base = datetime.datetime(2026, 1, 3, tzinfo=DEFAULT_TIMEZONE)
    first = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "safe",
    )
    moderated_user = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "blocked input",
        status=UTTERANCE_STATUS_MODERATED,
    )
    moderated_bot = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "blocked output",
        reply_to_id=moderated_user.id,
        status=UTTERANCE_STATUS_MODERATED,
    )
    final_bot = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "safe reply",
        reply_to_id=first.id,
    )
    first.timestamp = base
    moderated_user.timestamp = base + datetime.timedelta(seconds=1)
    moderated_bot.timestamp = base + datetime.timedelta(seconds=2)
    final_bot.timestamp = base + datetime.timedelta(seconds=3)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-4",
        up_to_timestamp=final_bot.timestamp,
    )

    assert [(msg.role.value, msg.content) for msg in history] == [
        ("user", "safe"),
        ("assistant", "safe reply"),
    ]
