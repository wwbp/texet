import datetime
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_api_key
from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
)
from app.db import get_async_session
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

API_KEY = "test-api-key"


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
        outbox.append({
            "user_id": user_id,
            "message": message,
            "utterance_id": utterance_id,
            "in_reply_to_utterance_id": in_reply_to_utterance_id,
        })

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
    sms_outbox: list[dict[str, str]],
) -> None:
    message = "a" * 10_000
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-large", "input": message},
    )
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
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["status"] == "queued"
    assert second_body["conversation_id"] == first_body["conversation_id"]

    assert sms_outbox == [
        {"user_id": "u1", "message": "reply:hello", "utterance_id": first_body["id"], "in_reply_to_utterance_id": first_body["user_utterance_id"]},
        {"user_id": "u1", "message": "reply:again", "utterance_id": second_body["id"], "in_reply_to_utterance_id": second_body["user_utterance_id"]},
    ]
    assert kani_stub[-1]["system_prompt"] == DEFAULT_SYSTEM_PROMPT
    assert kani_stub[0]["history"] == []
    assert kani_stub[1]["history"] == [("user", "hello"), ("assistant", "reply:hello")]

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
    assert response.status_code == 202
    body = response.json()
    assert body["conversation_id"] == conversation.id

    last = kani_stub[-1]
    assert last["history"] == [("user", "hello"), ("assistant", "hi")]


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
    assert bot_utterance.meta == {
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
async def test_response_creates_conversation_scoped_to_day_identifier(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-day-scope", "input": "hello", "metadata": {"day_identifier": 3}},
    )
    assert response.status_code == 202
    conv_id = response.json()["conversation_id"]

    async_session.expire_all()
    conv = await async_session.get(Conversation, conv_id)
    assert conv is not None
    assert conv.day_identifier == 3


@pytest.mark.asyncio
async def test_response_same_day_reuses_conversation(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    first = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-day-reuse", "input": "first", "metadata": {"day_identifier": 5}},
    )
    second = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-day-reuse", "input": "second", "metadata": {"day_identifier": 5}},
    )
    assert first.json()["conversation_id"] == second.json()["conversation_id"]


@pytest.mark.asyncio
async def test_response_different_day_creates_new_conversation(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    day1 = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-day-new", "input": "day one", "metadata": {"day_identifier": 1}},
    )
    day2 = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-day-new", "input": "day two", "metadata": {"day_identifier": 2}},
    )
    assert day1.status_code == 202
    assert day2.status_code == 202
    assert day1.json()["conversation_id"] != day2.json()["conversation_id"]

    async_session.expire_all()
    result = await async_session.execute(
        select(Conversation).where(Conversation.owner_speaker_id == "u-day-new")
    )
    conversations = result.scalars().all()
    assert len(conversations) == 2
    day_ids = {c.day_identifier for c in conversations}
    assert day_ids == {1, 2}


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
async def test_initial_message_appears_as_assistant_in_chat_history(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
    kani_stub: list[dict[str, object]],
) -> None:
    await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-history",
            "input": "Hello, welcome!",
            "metadata": {"is_initial": True},
        },
    )

    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-init-history", "input": "thanks"},
    )
    assert response.status_code == 202

    assert len(kani_stub) == 1
    history = kani_stub[0]["history"]
    assert history == [("assistant", "Hello, welcome!")]


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
    assert response.status_code == 202

    async_session.expire_all()
    utterance_result = await async_session.execute(select(Utterance))
    utterances = list(utterance_result.scalars().all())
    assert len(utterances) == 1
    assert utterances[0].status == UTTERANCE_STATUS_SENT


@pytest.mark.asyncio
async def test_initial_message_respects_day_identifier(
    async_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "user_id": "u-init-day",
            "input": "Day 3 greeting",
            "metadata": {"is_initial": True, "day_identifier": 3},
        },
    )
    assert response.status_code == 202
    conv_id = response.json()["conversation_id"]

    async_session.expire_all()
    conv = await async_session.get(Conversation, conv_id)
    assert conv is not None
    assert conv.day_identifier == 3


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
    assert init_response.status_code == 202
    init_conv_id = init_response.json()["conversation_id"]

    follow_response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-init-conv", "input": "hello back"},
    )
    assert follow_response.status_code == 202
    assert follow_response.json()["conversation_id"] == init_conv_id
