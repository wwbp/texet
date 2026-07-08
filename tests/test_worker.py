from __future__ import annotations

import asyncio
import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app import worker
from app.config import UTTERANCE_STATUS_FAILED, UTTERANCE_STATUS_SENT
from app.models.response import Utterance
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


async def _seed_queued_replies(
    session: AsyncSession, user_id: str, texts: list[str]
) -> list[Utterance]:
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


def _stub_externals(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        calls.append(query)
        return f"reply:{query}"

    async def _fake_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)


@pytest.mark.asyncio
async def test_process_one_returns_false_when_queue_empty(
    async_session: AsyncSession,
) -> None:
    assert await worker.process_one(_sessionmaker_from(async_session)) is False


@pytest.mark.asyncio
async def test_process_one_generates_and_sends_reply(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_externals(monkeypatch, calls)
    (bot_utt,) = await _seed_queued_replies(async_session, "u-w-one", ["hello"])

    assert await worker.process_one(_sessionmaker_from(async_session)) is True

    row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
    assert row is not None
    assert row.status == UTTERANCE_STATUS_SENT
    assert row.text == "reply:hello"
    assert row.attempts == 1
    assert calls == ["hello"]


@pytest.mark.asyncio
async def test_process_one_marks_reply_failed_on_error(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_externals(monkeypatch, calls)

    async def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(response_service, "_generate_reply", _boom)
    (bot_utt,) = await _seed_queued_replies(async_session, "u-w-fail", ["hello"])

    assert await worker.process_one(_sessionmaker_from(async_session)) is True

    row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
    assert row is not None
    assert row.status == UTTERANCE_STATUS_FAILED
    assert row.error is not None
    assert "llm exploded" in row.error


@pytest.mark.asyncio
async def test_process_one_preserves_per_user_order(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_externals(monkeypatch, calls)
    replies = await _seed_queued_replies(async_session, "u-w-order", ["one", "two"])
    sessionmaker = _sessionmaker_from(async_session)

    assert await worker.process_one(sessionmaker) is True
    assert await worker.process_one(sessionmaker) is True
    assert await worker.process_one(sessionmaker) is False

    assert calls == ["one", "two"]
    for bot_utt in replies:
        row = await async_session.get(Utterance, bot_utt.id, populate_existing=True)
        assert row is not None
        assert row.status == UTTERANCE_STATUS_SENT


@pytest.mark.asyncio
async def test_worker_loop_processes_until_shutdown(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _stub_externals(monkeypatch, calls)
    (bot_utt,) = await _seed_queued_replies(async_session, "u-w-loop", ["hello"])
    sessionmaker = _sessionmaker_from(async_session)

    shutdown = asyncio.Event()
    notify_event = asyncio.Event()
    task = asyncio.create_task(
        worker._worker_loop(sessionmaker, shutdown, notify_event, poll_interval=0.05)
    )

    async def _wait_for_sent() -> None:
        while True:
            async with sessionmaker() as session:
                row = await session.get(Utterance, bot_utt.id)
                if row is not None and row.status == UTTERANCE_STATUS_SENT:
                    return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait_for_sent(), timeout=5)
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)
    assert calls == ["hello"]