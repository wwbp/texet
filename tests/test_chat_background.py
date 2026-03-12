from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import MODERATION_VALUES_FOR_BLOCKED, UTTERANCE_STATUS_FAILED, UTTERANCE_STATUS_SENT
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


def _stub_moderation_openai(
    monkeypatch: pytest.MonkeyPatch,
    category_scores: dict[str, float] | None,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    response = SimpleNamespace(results=[SimpleNamespace(category_scores=category_scores)])

    class _FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            captured["api_key"] = api_key
            self.moderations = SimpleNamespace(create=self._create)

        def _create(self, *, input: str, model: str) -> SimpleNamespace:
            captured["input"] = input
            captured["model"] = model
            return response

    monkeypatch.setattr(response_service, "get_openai_api_key", lambda: "test-openai-key")
    monkeypatch.setattr(response_service, "OpenAI", _FakeOpenAI)
    return captured


@pytest.mark.asyncio
async def test_run_deferred_reply_success(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str]:
        return False, ""

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        return "ok"

    sent: dict[str, str] = {}

    async def _fake_send_sms(user_id: str, message: str, utterance_id: str) -> None:
        sent["user_id"] = user_id
        sent["message"] = message
        sent["utterance_id"] = utterance_id

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-bg-success", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-bg-success")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
        )
        bot_utterance = await create_queued_utterance(
            async_session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-success",
        user_utterance.id,
        bot_utterance_id,
        sessionmaker,
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_SENT
    assert refreshed.text == "ok"
    assert refreshed.error is None
    assert sent == {
        "user_id": "u-bg-success",
        "message": "ok",
        "utterance_id": bot_utterance_id,
    }


@pytest.mark.asyncio
async def test_run_deferred_reply_moderated_persists_and_sends(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_reason = "Blocked due to hate content with score 0.89."

    async def _fake_moderate_message(_utterance: Utterance) -> tuple[bool, str]:
        return True, blocked_reason

    async def _fail_generate_reply(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("generate reply should not run for moderated messages")

    sent: dict[str, str] = {}

    async def _fake_send_sms(user_id: str, message: str, utterance_id: str) -> None:
        sent["user_id"] = user_id
        sent["message"] = message
        sent["utterance_id"] = utterance_id

    monkeypatch.setattr(response_service, "_moderate_message", _fake_moderate_message)
    monkeypatch.setattr(response_service, "_generate_reply", _fail_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-bg-mod", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-bg-mod")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
        )
        bot_utterance = await create_queued_utterance(
            async_session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-mod",
        user_utterance.id,
        bot_utterance_id,
        sessionmaker,
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_SENT
    assert refreshed.text == blocked_reason
    assert refreshed.error is None
    assert sent == {
        "user_id": "u-bg-mod",
        "message": blocked_reason,
        "utterance_id": bot_utterance_id,
    }


@pytest.mark.asyncio
async def test_run_deferred_reply_failure_marks_failed(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str]:
        return False, ""

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-bg-fail", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-bg-fail")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hi",
        )
        bot_utterance = await create_queued_utterance(
            async_session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-fail",
        user_utterance.id,
        bot_utterance_id,
        sessionmaker,
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_FAILED
    assert refreshed.error and "boom" in refreshed.error


@pytest.mark.asyncio
async def test_send_sms_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(
            self,
            url: str,
            json: dict[str, str],
            headers: dict[str, str] | None = None,
        ) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(response_service, "get_sms_outbound_url", lambda: "https://sms.test")
    monkeypatch.setattr(
        response_service,
        "get_sms_outbound_authorization",
        lambda: "Bearer secure-test-token",
    )
    monkeypatch.setattr(response_service, "get_sms_timeout_seconds", lambda: 7.5)
    monkeypatch.setattr(response_service, "httpx", SimpleNamespace(AsyncClient=_FakeClient))

    await response_service._send_sms("u1", "hello", "utt-1")
    assert captured["url"] == "https://sms.test"
    assert captured["json"] == {
        "participant_id": "u1",
        "message": "hello",
        "message_type": "sent",
        "utterance_id": "utt-1",
    }
    assert captured["headers"] == {"Authorization": "Bearer secure-test-token"}
    assert captured["timeout"] == 7.5


@pytest.mark.asyncio
async def test_send_sms_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(response_service, "get_sms_outbound_url", lambda: "")
    with pytest.raises(RuntimeError):
        await response_service._send_sms("u1", "hello", "utt-1")


@pytest.mark.asyncio
async def test_moderate_message_allows_when_scores_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_moderation_openai(monkeypatch, None)
    utterance = Utterance(conversation_id="c-mod-1", speaker_id="u-mod-1", text="sample input")

    blocked, reason = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""
    assert captured == {
        "api_key": "test-openai-key",
        "input": "sample input",
        "model": "omni-moderation-latest",
    }


@pytest.mark.asyncio
async def test_moderate_message_blocks_when_score_exceeds_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threshold = MODERATION_VALUES_FOR_BLOCKED["harassment"]
    _stub_moderation_openai(monkeypatch, {"harassment": threshold + 0.01})
    utterance = Utterance(conversation_id="c-mod-2", speaker_id="u-mod-2", text="sample input")

    blocked, reason = await response_service._moderate_message(utterance)

    assert blocked is True
    assert "harassment" in reason
    assert f"{threshold + 0.01}" in reason


@pytest.mark.asyncio
async def test_moderate_message_allows_when_score_equals_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threshold = MODERATION_VALUES_FOR_BLOCKED["hate"]
    _stub_moderation_openai(monkeypatch, {"hate": threshold})
    utterance = Utterance(conversation_id="c-mod-3", speaker_id="u-mod-3", text="sample input")

    blocked, reason = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_moderate_message_allows_unknown_category_below_default_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_moderation_openai(monkeypatch, {"unknown/category": 0.99})
    utterance = Utterance(conversation_id="c-mod-4", speaker_id="u-mod-4", text="sample input")

    blocked, reason = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_moderate_message_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(response_service, "get_openai_api_key", lambda: "")
    utterance = Utterance(conversation_id="c-mod-5", speaker_id="u-mod-5", text="sample input")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set."):
        await response_service._moderate_message(utterance)


@pytest.mark.asyncio
async def test_moderate_message_requires_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(response_service, "get_openai_api_key", lambda: "test-openai-key")
    utterance = Utterance(conversation_id="c-mod-6", speaker_id="u-mod-6", text=None)

    with pytest.raises(RuntimeError, match="Utterance text is not set."):
        await response_service._moderate_message(utterance)
