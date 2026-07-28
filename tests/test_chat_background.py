from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from kani.models import ChatRole  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import (
    MODERATION_VALUES_FOR_BLOCKED,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_SENT,
)
from app.models.response import Utterance
from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.response.utils import day_marker


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


def _stub_moderation_openai(
    monkeypatch: pytest.MonkeyPatch,
    category_scores: dict[str, float] | None,
    results_list: list[object] | None = None,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    if results_list is not None:
        response = SimpleNamespace(results=results_list)
    else:
        response = SimpleNamespace(results=[SimpleNamespace(category_scores=category_scores)])

    class _FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            captured["api_key"] = api_key
            self.moderations = SimpleNamespace(create=self._create)

        async def _create(self, *, input: str, model: str) -> SimpleNamespace:
            captured["input"] = input
            captured["model"] = model
            return response

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(response_service, "get_openai_api_key", lambda: "test-openai-key")
    monkeypatch.setattr(response_service, "AsyncOpenAI", _FakeOpenAI)
    return captured


@pytest.mark.asyncio
async def test_run_deferred_reply_success(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        return "ok"

    sent: dict[str, str | None] = {}

    async def _fake_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        sent["user_id"] = user_id
        sent["message"] = message
        sent["utterance_id"] = utterance_id
        sent["in_reply_to_utterance_id"] = in_reply_to_utterance_id

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)
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
        user_utterance_id = user_utterance.id
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-success",
        user_utterance_id,
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
        "in_reply_to_utterance_id": user_utterance_id,
    }


@pytest.mark.asyncio
async def test_run_deferred_reply_moderated_persists_and_sends(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_reason = "I can't personally help with that, but your safety matters, and support is available. Call the crisis line at 988 to talk to someone."

    async def _fake_moderate_message(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return True, blocked_reason, "hate", 0.89

    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fail_generate_reply(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("generate reply should not run for moderated messages")

    sent: dict[str, str | None] = {}

    async def _fake_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        sent["user_id"] = user_id
        sent["message"] = message
        sent["utterance_id"] = utterance_id
        sent["in_reply_to_utterance_id"] = in_reply_to_utterance_id

    monkeypatch.setattr(response_service, "_moderate_message", _fake_moderate_message)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)
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
        user_utterance_id = user_utterance.id
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
    assert refreshed.status == UTTERANCE_STATUS_MODERATED
    assert refreshed.text == blocked_reason
    assert refreshed.error is None
    refreshed_user = await async_session.get(Utterance, user_utterance_id)
    assert refreshed_user is not None
    assert refreshed_user.status == UTTERANCE_STATUS_MODERATED
    assert sent == {
        "user_id": "u-bg-mod",
        "message": blocked_reason,
        "utterance_id": bot_utterance_id,
        "in_reply_to_utterance_id": user_utterance_id,
    }


@pytest.mark.asyncio
async def test_run_deferred_reply_failure_marks_failed(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Retry-until-exhausted is covered in tests/test_reply_retry.py; this test is
    # about the terminal state, so make the first error the last attempt.
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "1")

    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)
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
        user_utterance_id = user_utterance.id
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-fail",
        user_utterance_id,
        bot_utterance_id,
        sessionmaker,
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_FAILED
    assert refreshed.error and "boom" in refreshed.error


@pytest.mark.asyncio
async def test_run_deferred_reply_moderates_generated_reply_and_sends_notice(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_reply = "unsafe generated reply"
    moderation_notice = "A generated reply was moderated due to violence content with score 0.91."

    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        return raw_reply

    async def _block_generated_reply(text: str) -> tuple[bool, str, str, float]:
        assert text == raw_reply
        return True, "Blocked due to violence content with score 0.91.", "violence", 0.91

    sent: dict[str, str | None] = {}

    async def _fake_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        sent["user_id"] = user_id
        sent["message"] = message
        sent["utterance_id"] = utterance_id
        sent["in_reply_to_utterance_id"] = in_reply_to_utterance_id

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_moderate_text", _block_generated_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-bg-outbound-mod", meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(async_session, "u-bg-outbound-mod")
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
        user_utterance_id = user_utterance.id
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-bg-outbound-mod",
        user_utterance_id,
        bot_utterance_id,
        sessionmaker,
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_MODERATED
    assert refreshed.text == raw_reply
    assert refreshed.meta is not None
    assert refreshed.meta["texet_generation"]["query"] == "hi"
    assert refreshed.meta["texet_generation"]["chat_history"] == []
    assert {
        key: refreshed.meta[key]
        for key in (
            "texet_moderation_source",
            "texet_moderation_category",
            "texet_moderation_score",
            "texet_moderation_notice",
        )
    } == {
        "texet_moderation_source": "bot",
        "texet_moderation_category": "violence",
        "texet_moderation_score": 0.91,
        "texet_moderation_notice": moderation_notice,
    }
    assert sent == {
        "user_id": "u-bg-outbound-mod",
        "message": moderation_notice,
        "utterance_id": bot_utterance_id,
        "in_reply_to_utterance_id": user_utterance_id,
    }


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

    await response_service._send_sms("u1", "hello", "utt-1", "utt-user-1")
    assert captured["url"] == "https://sms.test"
    assert captured["json"] == {
        "participant_id": "u1",
        "message": "hello",
        "message_type": "sent",
        "utterance_id": "utt-1",
        "in_reply_to_utterance_id": "utt-user-1",
    }
    assert captured["headers"] == {"Authorization": "Bearer secure-test-token"}
    assert captured["timeout"] == 7.5


@pytest.mark.asyncio
async def test_send_sms_omits_in_reply_to_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

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
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(response_service, "get_sms_outbound_url", lambda: "https://sms.test")
    monkeypatch.setattr(response_service, "get_sms_outbound_authorization", lambda: None)
    monkeypatch.setattr(response_service, "get_sms_timeout_seconds", lambda: 5.0)
    monkeypatch.setattr(response_service, "httpx", SimpleNamespace(AsyncClient=_FakeClient))

    await response_service._send_sms("u1", "hello", "utt-1")
    assert "in_reply_to_utterance_id" not in captured["json"]


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

    blocked, reason, category, score = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""
    assert category == ""
    assert score == 0.0
    assert captured == {
        "api_key": "test-openai-key",
        "closed": True,
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

    blocked, reason, category, score = await response_service._moderate_message(utterance)

    assert blocked is True
    assert "harassment" in reason
    assert category == "harassment"
    assert score == pytest.approx(threshold + 0.01)


@pytest.mark.asyncio
async def test_moderate_message_allows_when_score_equals_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threshold = MODERATION_VALUES_FOR_BLOCKED["hate"]
    _stub_moderation_openai(monkeypatch, {"hate": threshold})
    utterance = Utterance(conversation_id="c-mod-3", speaker_id="u-mod-3", text="sample input")

    blocked, reason, category, score = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""
    assert category == ""
    assert score == 0.0


@pytest.mark.asyncio
async def test_moderate_message_allows_unknown_category_below_default_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_moderation_openai(monkeypatch, {"unknown/category": 0.99})
    utterance = Utterance(conversation_id="c-mod-4", speaker_id="u-mod-4", text="sample input")

    blocked, reason, category, score = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""
    assert category == ""
    assert score == 0.0


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


@pytest.mark.asyncio
async def test_run_deferred_reply_sms_failure_does_not_commit_text(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMS failure must leave the bot utterance with text=None, not partially committed."""
    # Retry-until-exhausted is covered in tests/test_reply_retry.py; this test is
    # about the terminal state, so make the first error the last attempt.
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "1")

    async def _allow_moderation(_utterance: Utterance) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_generate_reply(*_args: object, **_kwargs: object) -> str:
        return "the generated reply"

    async def _fail_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        raise RuntimeError("sms gateway down")

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fail_send_sms)

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-sms-fail", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-sms-fail")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(async_session, conversation.id, speaker.id, "hi")
        bot_utterance = await create_queued_utterance(
            async_session, conversation.id, bot.id, reply_to_id=user_utterance.id
        )
        user_utterance_id = user_utterance.id
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        "u-sms-fail", user_utterance_id, bot_utterance_id, sessionmaker
    )

    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_FAILED
    assert refreshed.error is not None and "sms gateway down" in refreshed.error
    assert refreshed.text is None  # rollback must have cleared the generated text


@pytest.mark.asyncio
async def test_moderate_text_empty_results_returns_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenAI occasionally returns an empty results list; must not IndexError.
    _stub_moderation_openai(monkeypatch, category_scores=None, results_list=[])
    utterance = Utterance(conversation_id="c-mod-7", speaker_id="u-mod-7", text="sample")

    blocked, reason, category, score = await response_service._moderate_message(utterance)

    assert blocked is False
    assert reason == ""
    assert category == ""
    assert score == 0.0


@pytest.mark.asyncio
async def test_initial_bot_message_included_in_history_not_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hub openings (texet_hub_initial=True) stay in chat history as assistant
    messages so the LLM sees the same transcript as the user. Bedrock's
    user-first/alternating-roles requirement is satisfied at the engine
    boundary by normalize_converse_messages (see test_engines.py), so an
    assistant-first history here is fine.
    """
    captured: dict[str, object] = {}

    async def _capturing_generate_reply(
        chat_history: list[object], query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        captured["chat_history"] = list(chat_history)
        captured["system_prompt"] = system_prompt
        return "reply"

    async def _allow_moderation(*_args: object, **_kwargs: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _fake_send_sms(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)
    monkeypatch.setattr(response_service, "_moderate_text", _allow_moderation)
    monkeypatch.setattr(response_service, "_generate_reply", _capturing_generate_reply)
    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)

    opening_text = "Hi! I'm your study buddy. How are you feeling today?"
    user_id = "u-initial-bot-fixed"

    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, user_id)
        conversation = await get_or_create_conversation(async_session, speaker.id)

        # Simulate is_initial=True: hub seeds the conversation with a tagged bot utterance.
        await create_utterance(
            async_session,
            conversation.id,
            bot.id,
            opening_text,
            status=UTTERANCE_STATUS_SENT,
            meta={"texet_hub_initial": True},
        )

        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "pretty good thanks",
        )
        bot_utterance = await create_queued_utterance(
            async_session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )
        user_utterance_id = user_utterance.id
        bot_utterance_id = bot_utterance.id

    sessionmaker = _sessionmaker_from(async_session)
    await response_service._run_deferred_reply(
        user_id,
        user_utterance_id,
        bot_utterance_id,
        sessionmaker,
    )

    # The reply should succeed — no Bedrock validation error.
    async_session.expire_all()
    refreshed = await async_session.get(Utterance, bot_utterance_id)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_SENT
    assert refreshed.error is None

    # The opening appears in history exactly as the user saw it, assistant-first
    # (prefixed with the day marker since it starts the day).
    today_marker = day_marker(datetime.datetime.now(datetime.UTC).date())
    history = captured.get("chat_history", [])
    assert [(msg.role, msg.content) for msg in history] == [
        (ChatRole.ASSISTANT, f"{today_marker}\n{opening_text}"),
    ]

    # And it is no longer duplicated into the system prompt.
    system_prompt = captured.get("system_prompt", "")
    assert opening_text not in str(system_prompt)
