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
def sms_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    outbox: list[dict[str, str]] = []

    async def _fake_send_sms(user_id: str, message: str, utterance_id: str) -> None:
        outbox.append({"user_id": user_id, "message": message, "utterance_id": utterance_id})

    monkeypatch.setattr(response_service, "_send_sms", _fake_send_sms)
    return outbox


@pytest.fixture(autouse=True)
def kani_stub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def _fake_generate_reply(
        chat_history: list[object], query: str, system_prompt: str
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
    async def _allow_moderation(_utterance: object) -> tuple[bool, str]:
        return False, ""

    monkeypatch.setattr(response_service, "_moderate_message", _allow_moderation)


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
async def test_response_allows_empty_input(
    async_client: AsyncClient,
    sms_outbox: list[dict[str, str]],
) -> None:
    response = await async_client.post(
        "/response",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"user_id": "u-empty", "input": ""},
    )
    assert response.status_code == 202
    body = response.json()
    assert sms_outbox == [{"user_id": "u-empty", "message": "reply:", "utterance_id": body["id"]}]


@pytest.mark.asyncio
async def test_response_allows_large_input(
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
    assert sms_outbox[0]["message"].startswith("reply:")
    assert len(sms_outbox[0]["message"]) == len(message) + 6
    assert sms_outbox[0]["utterance_id"] == body["id"]


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
        {"user_id": "u1", "message": "reply:hello", "utterance_id": first_body["id"]},
        {"user_id": "u1", "message": "reply:again", "utterance_id": second_body["id"]},
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
        base = datetime.datetime(2026, 1, 1, tzinfo=DEFAULT_TIMEZONE)
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
async def test_response_marks_failed_on_generation_error(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_generate_reply(
        chat_history: list[object], query: str, system_prompt: str
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
    async def _fail_send_sms(user_id: str, message: str, utterance_id: str) -> None:
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
