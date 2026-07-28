import datetime
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from kani.engines.base import BaseEngine  # type: ignore[import-untyped]
from kani.models import ChatMessage, ChatRole  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app import worker as reply_worker
from app.auth import hash_api_key
from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
)
from app.db import get_async_session
from app.engines.bedrock import _FIRST_TURN_PLACEHOLDER, BedrockCompletion, BedrockEngine
from app.main import app
from app.models.auth import ApiKey
from app.models.response import Conversation, Speaker, Utterance
from app.response import service as response_service
from app.response.crud import (
    DEFAULT_SYSTEM_PROMPT,
    bot_speaker_id,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.response.prompt import compose_instruction_prompt
from app.response.service import _generate_reply as real_generate_reply
from app.response.utils import day_marker

API_KEY = "test-api-key"


def _today_marker() -> str:
    """Day marker for messages created 'now' with no user timezone (UTC fallback)."""
    return day_marker(datetime.datetime.now(datetime.UTC).date())


async def _drain_replies(session: AsyncSession) -> None:
    """Process queued replies the way the worker service would in deployment."""
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    while await reply_worker.process_one(sessionmaker):
        pass


@pytest.fixture()
async def async_client(async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")

    async def _override_dependency() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    async with async_session.begin():
        async_session.add(
            ApiKey(
                name="test-key",
                key_hash=hash_api_key(API_KEY),
                key_prefix=API_KEY[:8],
                is_active=True,
            )
        )

    app.dependency_overrides[get_async_session] = _override_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def sms_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str | None]]:
    outbox: list[dict[str, str | None]] = []

    async def _fake_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        outbox.append(
            {
                "user_id": user_id,
                "message": message,
                "utterance_id": utterance_id,
                "in_reply_to_utterance_id": in_reply_to_utterance_id,
            }
        )

    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)
    return outbox


@pytest.fixture(autouse=True)
def kani_stub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        history = [
            (msg.role.value, msg.content)  # type: ignore[attr-defined]
            for msg in chat_history
        ]
        calls.append(
            {
                "query": query,
                "history_len": len(chat_history),
                "history": history,
                "system_prompt": system_prompt,
            }
        )
        return f"reply:{query}"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    return calls


@pytest.fixture(autouse=True)
def moderation_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow_moderation(_utterance: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)


@pytest.fixture(autouse=True)
def outbound_moderation_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow_text_moderation(_text: str) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    monkeypatch.setattr(response_service, "_moderate_text", _allow_text_moderation)


@pytest.fixture()
def moderation_email_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    outbox: list[dict[str, object]] = []

    async def _fake_send_moderation_email(
        user_id: str,
        utterance_id: str,
        conversation_id: str,
        speaker_id: str,
        utterance_text: str,
        utterance_timestamp: object,
        blocked_category: str,
        blocked_score: float,
        recent_chat_history: list[object],
    ) -> None:
        outbox.append(
            {
                "user_id": user_id,
                "utterance_id": utterance_id,
                "conversation_id": conversation_id,
                "speaker_id": speaker_id,
                "utterance_text": utterance_text,
                "utterance_timestamp": utterance_timestamp,
                "blocked_category": blocked_category,
                "blocked_score": blocked_score,
                "recent_chat_history": recent_chat_history,
            }
        )

    monkeypatch.setattr(response_service, "_send_moderation_email", _fake_send_moderation_email)
    return outbox


@pytest.mark.asyncio
async def test_response_requires_auth(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        json={"user_id": "u1", "input": "hello"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_response_rejects_invalid_key(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": "Bearer wrong-key"},
        json={"user_id": "u1", "input": "hello"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_response_validates_payload(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u1"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_response_rejects_unknown_mode(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u1", "input": "hello", "mode": "audio"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_response_rejects_empty_input(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-empty", "input": ""},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_response_rejects_oversized_input(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-oversized", "input": "a" * 10_001},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_response_accepts_max_length_input(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    message = "a" * 10_000
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-large", "input": message},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    assert len(sms_outbox) == 1
    body = response.json()
    assert sms_outbox[0]["user_id"] == "u-large"
    assert sms_outbox[0]["utterance_id"] == body["id"]
    assert sms_outbox[0]["in_reply_to_utterance_id"] == body["user_utterance_id"]


@pytest.mark.asyncio
async def test_response_success_persists(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    payload = {"user_id": "u1", "input": "hello"}
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=payload,
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    first_body = response.json()
    assert first_body["object"] == "response"
    assert first_body["status"] == "queued"
    assert first_body["mode"] == "text"
    assert len(first_body["conversation_id"]) == 32
    assert len(first_body["id"]) == 32
    assert len(first_body["user_utterance_id"]) == 32
    assert first_body["user_utterance_id"] != first_body["id"]

    second_payload = {"user_id": "u1", "input": "again"}
    second = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=second_payload,
    )
    await _drain_replies(async_session)
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["status"] == "queued"
    assert second_body["conversation_id"] == first_body["conversation_id"]

    assert sms_outbox == [
        {
            "user_id": "u1",
            "message": "reply:hello",
            "utterance_id": first_body["id"],
            "in_reply_to_utterance_id": first_body["user_utterance_id"],
        },
        {
            "user_id": "u1",
            "message": "reply:again",
            "utterance_id": second_body["id"],
            "in_reply_to_utterance_id": second_body["user_utterance_id"],
        },
    ]
    assert kani_stub[-1]["system_prompt"] == compose_instruction_prompt(DEFAULT_SYSTEM_PROMPT)
    assert kani_stub[0]["history"] == []
    assert kani_stub[1]["history"] == [
        ("user", f"{_today_marker()}\nhello"),
        ("assistant", "reply:hello"),
    ]

    async_session.expire_all()
    speaker_count = await async_session.execute(select(func.count()).select_from(Speaker))
    conversation_count = await async_session.execute(select(func.count()).select_from(Conversation))
    utterance_count = await async_session.execute(select(func.count()).select_from(Utterance))

    assert speaker_count.scalar_one() == 2
    assert conversation_count.scalar_one() == 1
    assert utterance_count.scalar_one() == 4

    status_counts = await async_session.execute(
        select(Utterance.status, func.count()).group_by(Utterance.status)
    )
    counts = {row[0]: row[1] for row in status_counts.all()}
    assert counts == {
        UTTERANCE_STATUS_RECEIVED: 2,
        UTTERANCE_STATUS_SENT: 2,
    }

    key_result = await async_session.execute(select(ApiKey).where(ApiKey.name == "test-key"))
    api_key = key_result.scalar_one()
    assert api_key.last_used_at is not None


@pytest.mark.asyncio
async def test_response_reuses_existing_conversation_history(
    async_client: AsyncClient,
    async_session: AsyncSession,
    kani_stub: list[dict[str, object]],
) -> None:
    async with async_session.begin():
        speaker = await get_or_create_speaker(async_session, "u-existing", meta={"type": "user"})
        bot = await get_or_create_bot_speaker(async_session, "u-existing")
        conversation = await get_or_create_conversation(async_session, speaker.id)
        user_utterance = await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hello",
        )
        bot_utterance = await create_utterance(
            async_session,
            conversation.id,
            bot.id,
            "hi",
            reply_to_id=user_utterance.id,
            status=UTTERANCE_STATUS_SENT,
        )
        # Timestamps must be within the current week so they are not
        # filtered out by the since_timestamp window in the pipeline.
        base = datetime.datetime.now(DEFAULT_TIMEZONE) - datetime.timedelta(hours=2)
        user_utterance.timestamp = base
        bot_utterance.timestamp = base + datetime.timedelta(seconds=1)

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-existing", "input": "follow-up"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    body = response.json()
    assert body["conversation_id"] == conversation.id

    last = kani_stub[-1]
    seeded_marker = day_marker(base.astimezone(datetime.UTC).date())
    assert last["history"] == [("user", f"{seeded_marker}\nhello"), ("assistant", "hi")]


@pytest.mark.asyncio
async def test_response_multiple_users_interleaved(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    desired_counts = {"u1": 3, "u2": 5, "u3": 7}
    order = [
        "u1",
        "u2",
        "u3",
        "u2",
        "u1",
        "u3",
        "u3",
        "u2",
        "u1",
        "u3",
        "u2",
        "u3",
        "u3",
        "u2",
        "u3",
    ]
    seen = dict.fromkeys(desired_counts, 0)
    conversation_ids: dict[str, str] = {}

    for user_id in order:
        seen[user_id] += 1
        payload = {"user_id": user_id, "input": f"msg-{user_id}-{seen[user_id]}"}
        response = await async_client.post(
            "/response",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
        )
        await _drain_replies(async_session)
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"

        if user_id not in conversation_ids:
            conversation_ids[user_id] = body["conversation_id"]
        else:
            assert body["conversation_id"] == conversation_ids[user_id]

    assert seen == desired_counts
    assert len(set(conversation_ids.values())) == 3

    total_requests = sum(desired_counts.values())
    assert len(sms_outbox) == total_requests
    assert len(kani_stub) == total_requests

    async_session.expire_all()
    speaker_count = await async_session.execute(select(func.count()).select_from(Speaker))
    conversation_count = await async_session.execute(select(func.count()).select_from(Conversation))
    utterance_count = await async_session.execute(select(func.count()).select_from(Utterance))

    assert speaker_count.scalar_one() == 6
    assert conversation_count.scalar_one() == 3
    assert utterance_count.scalar_one() == sum(desired_counts.values()) * 2

    per_convo = await async_session.execute(
        select(Utterance.conversation_id, func.count()).group_by(Utterance.conversation_id)
    )
    counts_by_convo = {row[0]: row[1] for row in per_convo.all()}
    assert counts_by_convo == {
        conversation_ids["u1"]: 6,
        conversation_ids["u2"]: 10,
        conversation_ids["u3"]: 14,
    }

    expected_history_len: dict[str, int] = {"u1": 0, "u2": 0, "u3": 0}
    for call in kani_stub:
        query = call["query"]
        assert isinstance(query, str)
        parts = query.split("-")
        assert len(parts) >= 3
        user_id = parts[1]
        assert user_id in expected_history_len
        assert call["history_len"] == expected_history_len[user_id]
        history = call["history"]
        assert isinstance(history, list)
        for role, content in history:
            assert isinstance(role, str)
            assert isinstance(content, str)
            assert f"-{user_id}-" in content
        expected_history_len[user_id] += 2

    utterance_rows = await async_session.execute(
        select(Utterance).where(Utterance.conversation_id.in_(conversation_ids.values()))
    )
    utterances = list(utterance_rows.scalars().all())
    utterances_by_id = {utterance.id: utterance for utterance in utterances}
    for utterance in utterances:
        if utterance.speaker_id.startswith("bot:"):
            assert utterance.reply_to_id is not None
            replied = utterances_by_id[utterance.reply_to_id]
            assert not replied.speaker_id.startswith("bot:")
            assert replied.conversation_id == utterance.conversation_id
            assert utterance.status == UTTERANCE_STATUS_SENT
            assert utterance.error is None
        else:
            assert utterance.status == UTTERANCE_STATUS_RECEIVED


@pytest.mark.asyncio
async def test_response_sends_moderation_email_when_blocked(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    moderation_email_outbox: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _block_moderation(_utterance: object) -> tuple[bool, str, str, float]:
        return True, "Blocked due to violence content with score 0.91.", "violence", 0.91

    monkeypatch.setattr(response_service, "_moderate_message", _block_moderation)

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-mod", "input": "blocked message"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    body = response.json()

    assert sms_outbox == [
        {
            "user_id": "u-mod",
            "message": "I can't personally help with that, but your safety matters, and support is available. Call the crisis line at 988 to talk to someone.",
            "utterance_id": body["id"],
            "in_reply_to_utterance_id": body["user_utterance_id"],
        }
    ]

    assert len(moderation_email_outbox) == 1
    email = moderation_email_outbox[0]
    assert email["user_id"] == "u-mod"
    assert email["blocked_category"] == "violence"
    assert email["blocked_score"] == pytest.approx(0.91)
    assert email["utterance_text"] == "blocked message"

    async_session.expire_all()
    user_result = await async_session.execute(
        select(Utterance).where(Utterance.speaker_id == "u-mod")
    )
    user_utterance = user_result.scalar_one()
    assert email["utterance_id"] == user_utterance.id
    assert email["conversation_id"] == user_utterance.conversation_id
    assert email["speaker_id"] == user_utterance.speaker_id
    assert user_utterance.status == UTTERANCE_STATUS_MODERATED

    recent_history = email["recent_chat_history"]
    assert isinstance(recent_history, list)
    assert 1 <= len(recent_history) <= 5
    last_message = recent_history[-1]
    assert last_message.content == "blocked message"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_response_does_not_fail_if_moderation_email_errors(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _block_moderation(_utterance: object) -> tuple[bool, str, str, float]:
        return True, "Blocked due to harassment content with score 0.73.", "harassment", 0.73

    async def _fail_send_moderation_email(
        user_id: str,
        utterance_id: str,
        conversation_id: str,
        speaker_id: str,
        utterance_text: str,
        utterance_timestamp: object,
        blocked_category: str,
        blocked_score: float,
        recent_chat_history: list[object],
    ) -> None:
        raise RuntimeError("mail down")

    monkeypatch.setattr(response_service, "_moderate_message", _block_moderation)
    monkeypatch.setattr(response_service, "_send_moderation_email", _fail_send_moderation_email)

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-mod-mail-fail", "input": "blocked message"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    body = response.json()

    assert sms_outbox == [
        {
            "user_id": "u-mod-mail-fail",
            "message": "I can't personally help with that, but your safety matters, and support is available. Call the crisis line at 988 to talk to someone.",
            "utterance_id": body["id"],
            "in_reply_to_utterance_id": body["user_utterance_id"],
        }
    ]

    async_session.expire_all()
    bot_result = await async_session.execute(select(Utterance).where(Utterance.id == body["id"]))
    bot_utterance = bot_result.scalar_one()
    assert bot_utterance.status == UTTERANCE_STATUS_MODERATED
    assert bot_utterance.error is None


@pytest.mark.asyncio
async def test_response_moderates_generated_reply_and_persists_raw_output(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_reply = "unsafe generated reply"
    moderation_notice = "A generated reply was moderated due to violence content with score 0.91."

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        return raw_reply

    async def _block_generated_reply(text: str) -> tuple[bool, str, str, float]:
        assert text == raw_reply
        return True, "Blocked due to violence content with score 0.91.", "violence", 0.91

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)
    monkeypatch.setattr(response_service, "_moderate_text", _block_generated_reply)

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-bot-mod", "input": "hello"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    body = response.json()

    assert sms_outbox == [
        {
            "user_id": "u-bot-mod",
            "message": moderation_notice,
            "utterance_id": body["id"],
            "in_reply_to_utterance_id": body["user_utterance_id"],
        }
    ]

    async_session.expire_all()
    bot_result = await async_session.execute(select(Utterance).where(Utterance.id == body["id"]))
    bot_utterance = bot_result.scalar_one()
    assert bot_utterance.status == UTTERANCE_STATUS_MODERATED
    assert bot_utterance.text == raw_reply
    assert bot_utterance.meta is not None
    assert bot_utterance.meta["texet_generation"]["query"] == "hello"
    assert bot_utterance.meta["texet_generation"]["chat_history"] == []
    assert {
        key: bot_utterance.meta[key]
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


@pytest.mark.asyncio
async def test_response_marks_failed_on_generation_error(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retry-until-exhausted is covered in tests/test_reply_retry.py; this test is
    # about the terminal state, so make the first error the last attempt.
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "1")

    async def _fail_generate_reply(
        chat_history: list[object], query: str, system_prompt: str, **_kwargs: object
    ) -> str:
        raise RuntimeError("kani down")

    monkeypatch.setattr(response_service, "_generate_reply", _fail_generate_reply)

    payload = {"user_id": "u8", "input": "hello"}
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=payload,
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    async_session.expire_all()
    result = await async_session.execute(
        select(Utterance).where(Utterance.speaker_id.like("bot:%"))
    )
    bot_utterance = result.scalar_one()
    assert bot_utterance.status == UTTERANCE_STATUS_FAILED
    assert bot_utterance.error is not None


@pytest.mark.asyncio
async def test_response_marks_failed_on_sms_error(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retry-until-exhausted is covered in tests/test_reply_retry.py; this test is
    # about the terminal state, so make the first error the last attempt.
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "1")

    async def _fail_send_sms(
        user_id: str,
        message: str,
        utterance_id: str,
        in_reply_to_utterance_id: str | None = None,
    ) -> None:
        raise RuntimeError("sms gateway down")

    monkeypatch.setattr(response_service, "_send_sms", _fail_send_sms)

    payload = {"user_id": "u9", "input": "hello"}
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=payload,
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    async_session.expire_all()
    result = await async_session.execute(
        select(Utterance).where(Utterance.speaker_id.like("bot:%"))
    )
    bot_utterance = result.scalar_one()
    assert bot_utterance.status == UTTERANCE_STATUS_FAILED
    assert bot_utterance.error is not None
    # Text must not be committed when SMS delivery fails — the DB row should
    # reflect only the failed state, not partial generation output.
    assert bot_utterance.text is None


@pytest.mark.asyncio
async def test_response_single_conversation_across_day_numbers(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    """Regression: per-day conversations used to reset chat history every day."""
    day1 = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-week", "input": "day one", "metadata": {"day_number": 1}},
    )
    await _drain_replies(async_session)
    day2 = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-week", "input": "day two", "metadata": {"day_number": 2}},
    )
    await _drain_replies(async_session)
    no_day = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-week", "input": "no metadata"},
    )
    await _drain_replies(async_session)
    assert day1.status_code == 202
    assert day2.status_code == 202
    assert no_day.status_code == 202
    assert (
        day1.json()["conversation_id"]
        == day2.json()["conversation_id"]
        == no_day.json()["conversation_id"]
    )

    async_session.expire_all()
    result = await async_session.execute(
        select(Conversation).where(Conversation.owner_speaker_id == "u-week")
    )
    conversations = result.scalars().all()
    assert len(conversations) == 1

    # The day-2 generation must see day 1's exchange in its chat history
    # (day markers may prefix the text).
    day2_history = kani_stub[1]["history"]
    assert any(role == "user" and "day one" in text for role, text in day2_history)
    assert ("assistant", "reply:day one") in day2_history
    assert kani_stub[2]["history_len"] == 4


@pytest.mark.asyncio
async def test_initial_message_persisted_as_bot_utterance(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init",
            "input": "Welcome to the study!",
            "metadata": {"is_initial": True},
        },
    )
    await _drain_replies(async_session)
    assert response.status_code == 202
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "recorded"
    assert body["mode"] == "text"
    assert len(body["id"]) == 32
    assert len(body["conversation_id"]) == 32

    assert sms_outbox == []
    assert kani_stub == []

    async_session.expire_all()
    utterance = await async_session.get(Utterance, body["id"])
    assert utterance is not None
    assert utterance.text == "Welcome to the study!"
    assert utterance.status == UTTERANCE_STATUS_SENT
    assert utterance.speaker_id == bot_speaker_id("u-init")
    assert utterance.reply_to_id is None
    assert utterance.meta is not None
    assert utterance.meta["texet_hub_initial"] is True


@pytest.mark.asyncio
async def test_initial_message_included_in_history_not_system_prompt(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    """Hub openings appear in chat history exactly as the user saw them.
    Assistant-first ordering is handled at the Bedrock engine boundary
    (normalize_converse_messages), not by hiding the message."""
    await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-history",
            "input": "Hello, welcome!",
            "metadata": {"is_initial": True},
        },
    )
    await _drain_replies(async_session)

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-init-history", "input": "thanks"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    assert len(kani_stub) == 1
    assert kani_stub[0]["history"] == [("assistant", f"{_today_marker()}\nHello, welcome!")]
    assert "Hello, welcome!" not in str(kani_stub[0]["system_prompt"])


@pytest.mark.asyncio
async def test_initial_message_does_not_create_queued_bot_utterance(
    async_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-count",
            "input": "Initial greeting",
            "metadata": {"is_initial": True},
        },
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    async_session.expire_all()
    utterance_result = await async_session.execute(select(Utterance))
    utterances = list(utterance_result.scalars().all())
    assert len(utterances) == 1
    assert utterances[0].status == UTTERANCE_STATUS_SENT


@pytest.mark.asyncio
async def test_initial_message_joins_single_conversation(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    initial = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-day",
            "input": "Day 3 greeting",
            "metadata": {"is_initial": True, "day_number": 3},
        },
    )
    await _drain_replies(async_session)
    assert initial.status_code == 202

    later = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-day",
            "input": "hello",
            "metadata": {"day_number": 4},
        },
    )
    await _drain_replies(async_session)
    assert later.status_code == 202
    assert later.json()["conversation_id"] == initial.json()["conversation_id"]


@pytest.mark.asyncio
async def test_normal_message_after_initial_uses_same_conversation(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    init_response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-conv",
            "input": "Hi there!",
            "metadata": {"is_initial": True},
        },
    )
    await _drain_replies(async_session)
    assert init_response.status_code == 202
    init_conv_id = init_response.json()["conversation_id"]

    follow_response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-init-conv", "input": "hello back"},
    )
    await _drain_replies(async_session)
    assert follow_response.status_code == 202
    assert follow_response.json()["conversation_id"] == init_conv_id


# ---------------------------------------------------------------------------
# End-to-end through the real Kani round (kani_stub bypassed)
# ---------------------------------------------------------------------------


class _CaptureEngine(BaseEngine):  # type: ignore[misc]
    """Minimal kani engine that records the exact messages it is handed."""

    max_context_size = 100_000

    def __init__(self) -> None:
        self.captured: list[ChatMessage] = []

    def message_len(self, message: ChatMessage) -> int:
        return 1

    async def prompt_len(
        self, messages: list[ChatMessage], functions: list | None = None, **kwargs: object
    ) -> int:
        return len(messages)

    async def predict(
        self, messages: list[ChatMessage], functions: list | None = None, **kwargs: object
    ) -> BedrockCompletion:
        self.captured = list(messages)
        return BedrockCompletion(ChatMessage(role=ChatRole.ASSISTANT, content="canned reply"))

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_e2e_hub_opening_flows_through_real_kani_round(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The autouse kani_stub bypasses kani entirely; this test restores the
    real _generate_reply and proves an assistant-first history survives the
    full Kani round."""
    monkeypatch.setattr(response_service, "_generate_reply", real_generate_reply)
    engine = _CaptureEngine()
    monkeypatch.setattr(response_service, "_create_engine", lambda *_a, **_k: engine)

    await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-real-kani",
            "input": "Hello, welcome!",
            "metadata": {"is_initial": True},
        },
    )
    await _drain_replies(async_session)
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-real-kani", "input": "thanks"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    assert [(msg.role, msg.content) for msg in engine.captured] == [
        (ChatRole.SYSTEM, compose_instruction_prompt(DEFAULT_SYSTEM_PROMPT)),
        (ChatRole.ASSISTANT, f"{_today_marker()}\nHello, welcome!"),
        (ChatRole.USER, "thanks"),
    ]
    assert sms_outbox[-1]["message"] == "canned reply"


@pytest.mark.asyncio
async def test_two_hub_openings_normalized_for_bedrock(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two back-to-back hub openings reach the Converse payload merged into a
    single assistant turn behind the placeholder user turn."""
    monkeypatch.setattr(response_service, "_generate_reply", real_generate_reply)
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="us.anthropic.claude-sonnet-4-6")
    captured: dict[str, list] = {}

    def _fake_call(system_blocks: list, messages: list) -> dict:
        captured["system"] = system_blocks
        captured["messages"] = messages
        return {"output": {"message": {"content": [{"text": "bedrock reply"}]}}}

    engine._call_bedrock = _fake_call  # type: ignore[method-assign]
    monkeypatch.setattr(response_service, "_create_engine", lambda *_a, **_k: engine)

    for opening in ("Opening one", "Opening two"):
        await async_client.post(
            "/response",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "user_id": "u-two-openings",
                "input": opening,
                "metadata": {"is_initial": True},
            },
        )
        await _drain_replies(async_session)
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-two-openings", "input": "hi there"},
    )
    await _drain_replies(async_session)
    assert response.status_code == 202

    assert captured["messages"] == [
        {"role": "user", "content": [{"text": _FIRST_TURN_PLACEHOLDER}]},
        {
            "role": "assistant",
            "content": [{"text": f"{_today_marker()}\nOpening one\n\nOpening two"}],
        },
        {"role": "user", "content": [{"text": "hi there"}]},
    ]
    assert sms_outbox[-1]["message"] == "bedrock reply"


@pytest.mark.asyncio
async def test_response_leaves_reply_queued_until_worker_runs(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-queued", "input": "hello"},
    )
    assert response.status_code == 202
    assert sms_outbox == []

    result = await async_session.execute(
        select(Utterance).where(Utterance.speaker_id == bot_speaker_id("u-queued"))
    )
    bot_utterance = result.scalar_one()
    assert bot_utterance.status == UTTERANCE_STATUS_QUEUED

    await _drain_replies(async_session)

    refreshed = await async_session.get(Utterance, bot_utterance.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.status == UTTERANCE_STATUS_SENT
    assert len(sms_outbox) == 1


@pytest.mark.asyncio
async def test_response_returns_503_when_reply_queue_is_full(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "1")

    first = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-bp-1", "input": "hello"},
    )
    assert first.status_code == 202

    second = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-bp-2", "input": "hello"},
    )
    assert second.status_code == 503
    assert second.headers["Retry-After"] == "30"

    await _drain_replies(async_session)

    third = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-bp-3", "input": "hello"},
    )
    assert third.status_code == 202


@pytest.mark.asyncio
async def test_response_backpressure_disabled_when_depth_is_zero(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "0")

    for i in range(3):
        response = await async_client.post(
            "/response",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"user_id": f"u-nobp-{i}", "input": "hello"},
        )
        assert response.status_code == 202
