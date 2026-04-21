from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_MODERATED
from app.response import service as response_service
from app.response.crud import (
    build_chat_history,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    get_weekly_summary,
)
from app.summary import service as summary_service
from app.summary.service import build_week_transcript, generate_user_weekly_summary

_WEEK_START = datetime.date(2026, 4, 12)
_WEEK_START_DT = datetime.datetime(2026, 4, 12, 0, 0, 0, tzinfo=datetime.UTC)
_WEEK_MID_DT = datetime.datetime(2026, 4, 14, 10, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# build_week_transcript — pure function
# ---------------------------------------------------------------------------


def test_build_week_transcript_formats_user_and_bot_lines() -> None:
    from app.models.response import Utterance

    user_utt = Utterance(speaker_id="alice", text="hello", status="received")
    bot_utt = Utterance(speaker_id="bot:alice", text="hi there", status="sent")

    transcript = build_week_transcript([user_utt, bot_utt], "alice")
    assert transcript == "user: hello\nbot: hi there"


def test_build_week_transcript_skips_moderated() -> None:
    from app.models.response import Utterance

    moderated = Utterance(speaker_id="alice", text="bad", status=UTTERANCE_STATUS_MODERATED)
    ok = Utterance(speaker_id="alice", text="good", status="received")

    transcript = build_week_transcript([moderated, ok], "alice")
    assert transcript == "user: good"


def test_build_week_transcript_skips_null_text() -> None:
    from app.models.response import Utterance

    null_utt = Utterance(speaker_id="alice", text=None, status="received")
    ok = Utterance(speaker_id="alice", text="hi", status="received")

    transcript = build_week_transcript([null_utt, ok], "alice")
    assert transcript == "user: hi"


def test_build_week_transcript_empty_returns_empty_string() -> None:
    assert build_week_transcript([], "alice") == ""


# ---------------------------------------------------------------------------
# generate_user_weekly_summary — requires DB + monkeypatched LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_user_weekly_summary_stores_result(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_generate_reply(
        _history: list[object], query: str, system_prompt: str
    ) -> str:
        assert "user: hello" in query
        assert "bot:" in query
        return "User greeted the bot and had a brief exchange."

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-sum-gen", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-sum-gen")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        utt1 = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hello",
            meta=None,
            status="received",
        )
        utt1.timestamp = _WEEK_MID_DT
        utt2 = await create_utterance(
            async_session,
            conversation.id,
            bot.id,
            "hi there",
            meta=None,
            status="sent",
        )
        utt2.timestamp = _WEEK_MID_DT

    await generate_user_weekly_summary(async_session, "u-sum-gen", _WEEK_START)

    summary = await get_weekly_summary(async_session, "u-sum-gen", _WEEK_START)
    assert summary == "User greeted the bot and had a brief exchange."


@pytest.mark.asyncio
async def test_generate_user_weekly_summary_skips_empty_week(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate_called = {"called": False}

    async def _should_not_be_called(*_args: object, **_kwargs: object) -> str:
        generate_called["called"] = True
        return "should not be called"

    monkeypatch.setattr(response_service, "_generate_reply", _should_not_be_called)

    async with async_session.begin():
        await get_or_create_speaker(async_session, "u-sum-empty", meta={"type": "user"})

    await generate_user_weekly_summary(async_session, "u-sum-empty", _WEEK_START)

    assert not generate_called["called"]
    assert await get_weekly_summary(async_session, "u-sum-empty", _WEEK_START) is None


@pytest.mark.asyncio
async def test_generate_user_weekly_summary_excludes_moderated_from_transcript(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    async def _fake_generate_reply(
        _history: list[object], query: str, _system_prompt: str
    ) -> str:
        captured["query"] = query
        return "clean summary"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-sum-mod", meta={"type": "user"})
        conversation = await get_or_create_conversation(async_session, speaker.id)
        utt1 = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "bad content",
            status=UTTERANCE_STATUS_MODERATED,
        )
        utt1.timestamp = _WEEK_MID_DT
        utt2 = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "clean message",
            status="received",
        )
        utt2.timestamp = _WEEK_MID_DT

    await generate_user_weekly_summary(async_session, "u-sum-mod", _WEEK_START)

    assert "bad content" not in captured.get("query", "")
    assert "clean message" in captured.get("query", "")


@pytest.mark.asyncio
async def test_generate_user_weekly_summary_is_idempotent(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = {"n": 0}

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        call_count["n"] += 1
        return f"summary v{call_count['n']}"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-sum-idem", meta={"type": "user"})
        conversation = await get_or_create_conversation(async_session, speaker.id)
        utt = await create_utterance(
            async_session, conversation.id, speaker.id, "hi", status="received"
        )
        utt.timestamp = _WEEK_MID_DT

    await generate_user_weekly_summary(async_session, "u-sum-idem", _WEEK_START)
    await generate_user_weekly_summary(async_session, "u-sum-idem", _WEEK_START)

    result = await get_weekly_summary(async_session, "u-sum-idem", _WEEK_START)
    assert result == "summary v2"


# ---------------------------------------------------------------------------
# build_chat_history with since_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_chat_history_since_timestamp_excludes_prior_week(
    async_session: AsyncSession,
) -> None:
    last_week_dt = datetime.datetime(2026, 4, 11, 10, 0, 0, tzinfo=datetime.UTC)
    this_week_dt = datetime.datetime(2026, 4, 13, 10, 0, 0, tzinfo=datetime.UTC)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-hist-since", meta={"type": "user"})
        conversation = await get_or_create_conversation(async_session, speaker.id)
        old_utt = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "old message",
            status="received",
        )
        old_utt.timestamp = last_week_dt
        new_utt = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "new message",
            status="received",
        )
        new_utt.timestamp = this_week_dt

    history = await build_chat_history(
        async_session,
        conversation_id=conversation.id,
        user_id="u-hist-since",
        up_to_timestamp=datetime.datetime(2026, 4, 19, 0, 0, 0, tzinfo=datetime.UTC),
        since_timestamp=_WEEK_START_DT,
    )

    texts = [m.content for m in history]
    assert "new message" in texts
    assert "old message" not in texts
