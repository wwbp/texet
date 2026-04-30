"""
Service-level integration tests for daily prompt lookup and instruction prompt persistence.
Follows the same pattern as test_chat_background.py: real DB via async_session,
all external calls (generate, SMS, moderation) monkeypatched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.response import Conversation, DailyPrompt
from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.response.prompt import compose_instruction_prompt


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


def _stub_pass_through(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stubs out all external calls so _run_deferred_reply succeeds cleanly."""
    captured: dict[str, object] = {}

    async def _allow(_utterance: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate(
        chat_history: object, query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        captured["system_prompt"] = system_prompt
        return "ok"

    async def _fake_sms(user_id: str, message: str, utterance_id: str) -> None:
        captured["sent"] = message

    monkeypatch.setattr(response_service, "_moderate_message", _allow)
    monkeypatch.setattr(response_service, "_moderate_text", _allow)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate)
    monkeypatch.setattr(response_service, "_send_sms", _fake_sms)
    return captured


@pytest.mark.asyncio
async def test_daily_prompt_appended_when_day_identifier_matches(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        async_session.add(DailyPrompt(day_identifier=5, content="Do breathing exercises."))

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-match", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-match")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"day_identifier": 5},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-match", user_utterance.id, bot_utterance.id, sessionmaker
    )

    assert "[Daily Activity]" in str(captured.get("system_prompt"))
    assert "Do breathing exercises." in str(captured.get("system_prompt"))


@pytest.mark.asyncio
async def test_no_daily_prompt_when_identifier_unmatched(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-miss", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-miss")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"day_identifier": 99},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-miss", user_utterance.id, bot_utterance.id, sessionmaker
    )

    assert "[Daily Activity]" not in str(captured.get("system_prompt"))


@pytest.mark.asyncio
async def test_no_daily_prompt_when_no_identifier_in_meta(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        async_session.add(DailyPrompt(day_identifier=1, content="Should not appear."))

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-nometa", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-nometa")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session, conversation.id, speaker.id, "hi", meta=None
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-nometa", user_utterance.id, bot_utterance.id, sessionmaker
    )

    assert "[Daily Activity]" not in str(captured.get("system_prompt"))
    assert "Should not appear." not in str(captured.get("system_prompt"))


@pytest.mark.asyncio
async def test_instruction_prompt_saved_to_conversation_meta(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pass_through(monkeypatch)

    async with async_session.begin():
        async_session.add(DailyPrompt(day_identifier=2, content="Day 2 activity."))

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-meta", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-meta")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        conv_id = conversation.id
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"day_identifier": 2},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-meta", user_utterance.id, bot_utterance.id, sessionmaker
    )

    async_session.expire_all()
    conv = await async_session.get(Conversation, conv_id)
    assert conv is not None
    assert conv.meta is not None
    assert "texet_instruction_prompt" in conv.meta
    assert "[Daily Activity]" in conv.meta["texet_instruction_prompt"]
    assert conv.meta.get("texet_day_identifier") == 2


@pytest.mark.asyncio
async def test_instruction_prompt_saved_without_day_identifier(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pass_through(monkeypatch)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-base", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-base")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        conv_id = conversation.id
        user_utterance = await create_utterance(
            async_session, conversation.id, speaker.id, "hi", meta=None
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-base", user_utterance.id, bot_utterance.id, sessionmaker
    )

    async_session.expire_all()
    conv = await async_session.get(Conversation, conv_id)
    assert conv is not None
    assert conv.meta is not None
    assert "texet_instruction_prompt" in conv.meta
    assert "texet_day_identifier" not in conv.meta


@pytest.mark.asyncio
async def test_compose_instruction_prompt_sections_order() -> None:
    result = compose_instruction_prompt(
        base="System base.",
        daily_content="Activity for today.",
        weekly_summary="Summary of last week.",
    )
    base_pos = result.index("System base.")
    daily_pos = result.index("[Daily Activity]")
    summary_pos = result.index("[Previous week summary]")
    assert base_pos < daily_pos < summary_pos
