from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_pipeline_injects_summary_into_system_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a previous-week summary exists, it is appended to the system prompt."""
    captured: dict[str, object] = {}

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str
    ) -> str:
        captured["system_prompt"] = system_prompt
        captured["history_len"] = len(chat_history)
        return "ok"

    async def _allow_moderation(*_args: object, **_kwargs: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_send_sms(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    # Compute last Sunday so the summary lands in "previous week"
    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    prev_week_start = current_week_start - datetime.timedelta(days=7)

    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-pipe-sum", meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(async_session, "u-pipe-sum")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        await upsert_weekly_summary(
            async_session, "u-pipe-sum", prev_week_start, "User discussed their goals."
        )
        user_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "hello"
        )
        bot_utt = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utt.id
        )
        bot_utt_id = bot_utt.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-pipe-sum", user_utt.id, bot_utt_id, sessionmaker
    )

    system_prompt = captured["system_prompt"]
    assert isinstance(system_prompt, str)
    assert "[Previous week summary]" in system_prompt
    assert "User discussed their goals." in system_prompt


@pytest.mark.asyncio
async def test_pipeline_uses_base_prompt_when_no_summary(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a prior summary the base system prompt is passed unchanged."""
    captured: dict[str, object] = {}

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str
    ) -> str:
        captured["system_prompt"] = system_prompt
        return "ok"

    async def _allow_moderation(*_args: object, **_kwargs: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_send_sms(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-pipe-nosum", meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(async_session, "u-pipe-nosum")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "hello"
        )
        bot_utt = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utt.id
        )
        bot_utt_id = bot_utt.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-pipe-nosum", user_utt.id, bot_utt_id, sessionmaker
    )

    system_prompt = captured["system_prompt"]
    assert isinstance(system_prompt, str)
    assert "[Previous week summary]" not in system_prompt


@pytest.mark.asyncio
async def test_pipeline_chat_history_limited_to_current_week(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Utterances from before this week's Sunday are excluded from chat history."""
    captured: dict[str, object] = {}

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str
    ) -> str:
        captured["history"] = [(m.role.value, m.content) for m in chat_history]  # type: ignore[attr-defined]
        return "ok"

    async def _allow_moderation(*_args: object, **_kwargs: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_send_sms(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    last_week_ts = datetime.datetime.combine(
        current_week_start - datetime.timedelta(days=1),
        datetime.time(12, 0, 0),
        tzinfo=datetime.UTC,
    )
    this_week_ts = datetime.datetime.combine(
        current_week_start + datetime.timedelta(days=1),
        datetime.time(9, 0, 0),
        tzinfo=datetime.UTC,
    )

    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-pipe-hist", meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(async_session, "u-pipe-hist")
        conversation = await get_or_create_conversation(async_session, speaker.id)

        old_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "last week message"
        )
        old_utt.timestamp = last_week_ts

        new_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "this week message"
        )
        new_utt.timestamp = this_week_ts

        current_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "current query"
        )
        bot_utt = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=current_utt.id
        )
        bot_utt_id = bot_utt.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-pipe-hist", current_utt.id, bot_utt_id, sessionmaker
    )

    history_texts = [text for _role, text in captured.get("history", [])]
    assert "this week message" in history_texts
    assert "last week message" not in history_texts
