"""Bot replies record which config produced them, and when they were sent."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import UTTERANCE_STATUS_MODERATED, UTTERANCE_STATUS_SENT
from app.models.response import (
    DailyPrompt,
    InstructionTemplate,
    SystemPrompt,
    Utterance,
    WeeklySummary,
)
from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.response.utils import prompt_sha256, week_start_for


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    assert bind is not None
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    block_user: bool = False,
    block_bot: bool = False,
) -> dict[str, str]:
    captured: dict[str, str] = {}

    async def _moderate_message(*_a: object) -> tuple[bool, str, str, float]:
        return (True, "x", "violence", 0.9) if block_user else (False, "", "", 0.0)

    async def _moderate_text(*_a: object) -> tuple[bool, str, str, float]:
        return (True, "x", "violence", 0.9) if block_bot else (False, "", "", 0.0)

    async def _generate_reply(
        _history: object, _query: str, system_prompt: str, **_k: object
    ) -> str:
        captured["system_prompt"] = system_prompt
        return "ok"

    async def _send_sms(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _moderate_message)
    monkeypatch.setattr(response_service, "_moderate_text", _moderate_text)
    monkeypatch.setattr(response_service, "_generate_reply", _generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _send_sms)
    return captured


async def _run(async_session: AsyncSession, user_id: str, day_number: int | None) -> Utterance:
    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, user_id)
        conversation = await get_or_create_conversation(async_session, speaker.id)
        meta = {"day_number": day_number} if day_number is not None else None
        user_utt = await create_utterance(
            async_session, conversation.id, speaker.id, "hi", meta=meta
        )
        bot_utt = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utt.id
        )
        user_utt_id, bot_utt_id = user_utt.id, bot_utt.id

    await response_service._run_deferred_reply(
        user_id, user_utt_id, bot_utt_id, _sessionmaker_from(async_session)
    )
    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utt_id)
    assert refreshed is not None and refreshed.meta is not None
    return refreshed


async def _seed_config(async_session: AsyncSession, user_id: str) -> dict[str, str]:
    week = week_start_for(datetime.datetime.now(datetime.UTC), None)
    async with async_session.begin():
        await get_or_create_speaker(async_session, user_id, meta={"type": "user"})
        rows = [
            SystemPrompt(id="sp-old", prompt="old", provider="bedrock", model_id="m-old"),
            InstructionTemplate(id="tpl", template="{base}|{weekly_summary}"),
            DailyPrompt(id="dp2", day_number=2, content="day two"),
            WeeklySummary(
                id="ws-prev",
                user_id=user_id,
                week_start=week - datetime.timedelta(days=7),
                summary="last week",
            ),
        ]
        rows[0].created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        async_session.add_all(rows)
        async_session.add(SystemPrompt(id="sp-new", prompt="new", provider="bedrock", model_id="m"))
    return {"week": week.isoformat()}


@pytest.mark.asyncio
async def test_sent_reply_records_provenance_and_sent_at(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub(monkeypatch)
    seeded = await _seed_config(async_session, "u-prov")
    before = datetime.datetime.now(datetime.UTC)

    reply = await _run(async_session, "u-prov", day_number=2)

    assert reply.status == UTTERANCE_STATUS_SENT
    assert reply.sent_at is not None and reply.sent_at >= before
    meta = reply.meta
    assert meta is not None
    assert meta["texet_provider"] == "bedrock"
    assert meta["texet_model_id"] == "m"
    assert meta["texet_system_prompt_id"] == "sp-new"
    assert meta["texet_instruction_template_id"] == "tpl"
    assert meta["texet_daily_prompt_id"] == "dp2"
    assert "texet_daily_prompt_missing" not in meta
    assert meta["texet_weekly_summary_id"] == "ws-prev"
    assert meta["texet_week_start"] == seeded["week"]
    assert meta["texet_day_number"] == 2
    assert meta["texet_system_prompt_sha256"] == prompt_sha256(captured["system_prompt"])


@pytest.mark.asyncio
async def test_defaults_and_missing_daily_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch)
    reply = await _run(async_session, "u-prov-default", day_number=9)
    meta = reply.meta
    assert meta is not None
    assert meta["texet_system_prompt_id"] is None
    assert meta["texet_instruction_template_id"] is None
    assert meta["texet_daily_prompt_id"] is None
    assert meta["texet_daily_prompt_missing"] is True
    assert meta["texet_weekly_summary_id"] is None


@pytest.mark.asyncio
async def test_blank_system_prompt_row_counts_as_default(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch)
    async with async_session.begin():
        async_session.add(
            SystemPrompt(id="sp-blank", prompt="  ", provider="bedrock", model_id="m")
        )
    reply = await _run(async_session, "u-prov-blank", day_number=None)
    assert reply.meta is not None
    assert reply.meta["texet_system_prompt_id"] is None
    assert reply.meta["texet_model_id"] == "m"


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["user", "bot"])
async def test_moderated_replies_get_the_same_fields(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    _stub(monkeypatch, block_user=who == "user", block_bot=who == "bot")
    await _seed_config(async_session, f"u-prov-mod-{who}")
    reply = await _run(async_session, f"u-prov-mod-{who}", day_number=2)
    assert reply.status == UTTERANCE_STATUS_MODERATED
    assert reply.sent_at is not None
    assert reply.meta is not None
    assert reply.meta["texet_moderation_source"] == who
    assert reply.meta["texet_system_prompt_id"] == "sp-new"
    assert reply.meta["texet_daily_prompt_id"] == "dp2"
    assert reply.meta["texet_weekly_summary_id"] == "ws-prev"
    assert len(reply.meta["texet_system_prompt_sha256"]) == 64
