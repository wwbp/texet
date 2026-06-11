import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
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
async def test_closed_conversation_does_not_block_new_open_one(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-closed", meta={"type": "user"})

    first = await get_or_create_conversation(async_session, speaker.id)
    first.status = "closed"
    await async_session.commit()

    second = await get_or_create_conversation(async_session, speaker.id)
    await async_session.commit()

    assert second.id != first.id
    count = await async_session.execute(select(func.count()).select_from(Conversation))
    assert count.scalar_one() == 2


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
        status=UTTERANCE_STATUS_SENT,
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
        status=UTTERANCE_STATUS_SENT,
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
        status=UTTERANCE_STATUS_SENT,
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
        status=UTTERANCE_STATUS_SENT,
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


@pytest.mark.asyncio
async def test_build_chat_history_includes_hub_initial_as_assistant(
    async_session: AsyncSession,
) -> None:
    speaker = await get_or_create_speaker(async_session, "user-5", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-5")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    base = datetime.datetime(2026, 1, 4, tzinfo=DEFAULT_TIMEZONE)
    opening = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "Daily check-in: how did you sleep?",
        meta={"texet_hub_initial": True},
        status=UTTERANCE_STATUS_SENT,
    )
    answer = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "Pretty well",
    )
    opening.timestamp = base
    answer.timestamp = base + datetime.timedelta(seconds=1)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-5",
        up_to_timestamp=answer.timestamp,
    )

    assert [(msg.role.value, msg.content) for msg in history] == [
        ("assistant", "Daily check-in: how did you sleep?"),
        ("user", "Pretty well"),
    ]


@pytest.mark.asyncio
async def test_build_chat_history_only_sent_bot_utterances(
    async_session: AsyncSession,
) -> None:
    """Bot messages the user never received (failed, queued, received) are
    excluded; only delivered (sent) ones appear."""
    speaker = await get_or_create_speaker(async_session, "user-6", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-6")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    base = datetime.datetime(2026, 1, 5, tzinfo=DEFAULT_TIMEZONE)
    user_msg = await create_utterance(async_session, conversation.id, speaker.id, "hello")
    sent_bot = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "delivered reply",
        reply_to_id=user_msg.id,
        status=UTTERANCE_STATUS_SENT,
    )
    failed_bot = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "undelivered reply",
        status=UTTERANCE_STATUS_FAILED,
    )
    received_bot = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "never finalized",
        status=UTTERANCE_STATUS_RECEIVED,
    )
    user_msg.timestamp = base
    sent_bot.timestamp = base + datetime.timedelta(seconds=1)
    failed_bot.timestamp = base + datetime.timedelta(seconds=2)
    received_bot.timestamp = base + datetime.timedelta(seconds=3)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-6",
        up_to_timestamp=received_bot.timestamp,
    )

    assert [(msg.role.value, msg.content) for msg in history] == [
        ("user", "hello"),
        ("assistant", "delivered reply"),
    ]


@pytest.mark.asyncio
async def test_build_chat_history_day_markers_from_user_local_time(
    async_session: AsyncSession,
) -> None:
    """A marker prefixes the first message of each user-local calendar day."""
    speaker = await get_or_create_speaker(async_session, "user-7", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-7")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    # UTC timestamps; the -5h offset shifts day boundaries.
    monday_msg = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "monday morning",
        meta={"user_local_time": "2026-01-05T09:00:00-05:00"},
    )
    monday_reply = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "good morning",
        status=UTTERANCE_STATUS_SENT,
    )
    tuesday_msg = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "tuesday evening",
        meta={"user_local_time": "2026-01-06T19:00:00-05:00"},
    )
    monday_msg.timestamp = datetime.datetime(2026, 1, 5, 14, 0, tzinfo=datetime.UTC)
    monday_reply.timestamp = datetime.datetime(2026, 1, 5, 14, 1, tzinfo=datetime.UTC)
    tuesday_msg.timestamp = datetime.datetime(2026, 1, 7, 0, 0, tzinfo=datetime.UTC)
    await async_session.commit()

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-7",
        up_to_timestamp=tuesday_msg.timestamp,
        annotate_days=True,
    )

    # Jan 7 00:00 UTC is still Tuesday Jan 6 at UTC-5.
    assert [(msg.role.value, msg.content) for msg in history] == [
        ("user", "[Monday, January 5]\nmonday morning"),
        ("assistant", "good morning"),
        ("user", "[Tuesday, January 6]\ntuesday evening"),
    ]


@pytest.mark.asyncio
async def test_build_chat_history_day_marker_offset_backfills_leading_messages(
    async_session: AsyncSession,
) -> None:
    """A hub opening with no offset of its own uses the first known offset, and
    without annotate_days no markers appear at all."""
    speaker = await get_or_create_speaker(async_session, "user-8", meta={"type": "user"})
    bot = await get_or_create_bot_speaker(async_session, "user-8")
    conversation = await create_conversation(async_session, speaker.id)
    await async_session.commit()

    opening = await create_utterance(
        async_session,
        conversation.id,
        bot.id,
        "daily check-in",
        meta={"texet_hub_initial": True},
        status=UTTERANCE_STATUS_SENT,
    )
    answer = await create_utterance(
        async_session,
        conversation.id,
        speaker.id,
        "doing fine",
        meta={"user_local_time": "2026-01-05T20:30:00-05:00"},
    )
    # 01:00 UTC on Jan 6 is still Jan 5 at the user's UTC-5 offset; without
    # the backfill the opening would be marked a day ahead of the answer.
    opening.timestamp = datetime.datetime(2026, 1, 6, 1, 0, tzinfo=datetime.UTC)
    answer.timestamp = datetime.datetime(2026, 1, 6, 1, 30, tzinfo=datetime.UTC)
    await async_session.commit()

    annotated = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-8",
        up_to_timestamp=answer.timestamp,
        annotate_days=True,
    )
    assert [(msg.role.value, msg.content) for msg in annotated] == [
        ("assistant", "[Monday, January 5]\ndaily check-in"),
        ("user", "doing fine"),
    ]

    plain = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="user-8",
        up_to_timestamp=answer.timestamp,
    )
    assert [(msg.role.value, msg.content) for msg in plain] == [
        ("assistant", "daily check-in"),
        ("user", "doing fine"),
    ]
