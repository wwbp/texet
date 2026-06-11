"""
Service-level integration tests for daily prompt lookup and instruction prompt persistence.
Follows the same pattern as test_chat_background.py: real DB via async_session,
all external calls (generate, SMS, moderation) monkeypatched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.response import Conversation, DailyPrompt, Utterance
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

    async def _fake_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        captured["sent"] = message

    monkeypatch.setattr(response_service, "_moderate_message", _allow)
    monkeypatch.setattr(response_service, "_moderate_text", _allow)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate)
    monkeypatch.setattr(response_service, "_send_sms", _fake_sms)
    return captured


@pytest.mark.asyncio
async def test_daily_prompt_appended_when_day_number_matches(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=5, content="Do breathing exercises."))

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-dp-match", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-dp-match")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"day_number": 5},
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
            meta={"day_number": 99},
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
        async_session.add(DailyPrompt(day_number=1, content="Should not appear."))

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
async def test_prompt_data_recorded_on_bot_utterance_not_conversation(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pass_through(monkeypatch)

    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=2, content="Day 2 activity."))

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
            meta={"day_number": 2},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )
        bot_utt_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-dp-meta", user_utterance.id, bot_utt_id, sessionmaker
    )

    async_session.expire_all()
    conv = await async_session.get(Conversation, conv_id)
    assert conv is not None
    assert conv.meta is None

    bot_utt = await async_session.get(Utterance, bot_utt_id)
    assert bot_utt is not None
    assert bot_utt.meta is not None
    snapshot = bot_utt.meta["texet_generation"]
    assert "[Daily Activity]" in snapshot["system_prompt"]
    assert snapshot["day_number"] == 2


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


@pytest.mark.asyncio
async def test_user_local_time_in_system_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-ult-present", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-ult-present")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"user_local_time": "2026-06-07T14:30:00-05:00"},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-ult-present", user_utterance.id, bot_utterance.id, sessionmaker
    )

    prompt = str(captured.get("system_prompt"))
    assert "[User's Local Time]" in prompt
    assert "Sunday, June 7, 2026 at 2:30 PM (UTC-5)" in prompt


@pytest.mark.asyncio
async def test_user_local_time_recorded_in_generation_snapshot(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pass_through(monkeypatch)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-ult-meta", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-ult-meta")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
            meta={"user_local_time": "2026-06-07T14:30:00-05:00"},
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )
        bot_utt_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-ult-meta", user_utterance.id, bot_utt_id, sessionmaker
    )

    async_session.expire_all()
    bot_utt = await async_session.get(Utterance, bot_utt_id)
    assert bot_utt is not None
    assert bot_utt.meta is not None
    snapshot = bot_utt.meta["texet_generation"]
    assert snapshot["user_local_time"] == "2026-06-07T14:30:00-05:00"


@pytest.mark.asyncio
async def test_user_local_time_absent_excluded_from_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_pass_through(monkeypatch)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-ult-absent", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-ult-absent")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session, conversation.id, speaker.id, "hi", meta=None
        )
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-ult-absent", user_utterance.id, bot_utterance.id, sessionmaker
    )

    # The conventions section mentions the label; check the section itself is absent.
    assert "The user's current local time is" not in str(captured.get("system_prompt"))
