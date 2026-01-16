from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
)
from app.db import get_async_session
from app.main import app
from app.models import Conversation, Speaker, Utterance
from app.services import chat as chat_service


@pytest.fixture()
async def async_client(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    monkeypatch.setenv("API_TOKEN", "test-token")

    async def _override_dependency() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def sms_outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    outbox: list[dict[str, str]] = []

    async def _fake_send_sms(payload: chat_service.SmsOutboundRequest) -> None:
        outbox.append(payload.model_dump())

    monkeypatch.setattr(chat_service, "send_sms", _fake_send_sms)
    return outbox


@pytest.mark.asyncio
async def test_chat_requires_auth(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/chat",
        json={"user_id": "u1", "message": "hello"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_validates_payload(async_client: AsyncClient) -> None:
    response = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json={"user_id": "u1"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_success_persists(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
) -> None:
    payload = {"user_id": "u1", "message": "hello"}
    first = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    assert first.status_code == 202
    first_body = first.json()
    assert first_body["status"] == "queued"
    assert len(first_body["conversation_id"]) == 32
    assert len(first_body["reply_utterance_id"]) == 32
    assert "text" not in first_body

    second_payload = {"user_id": "u1", "message": "again"}
    second = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=second_payload,
    )
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["status"] == "queued"
    assert second_body["conversation_id"] == first_body["conversation_id"]

    assert len(sms_outbox) == 2
    assert sms_outbox[0] == {"user_id": "u1", "message": "echo:hello"}
    assert sms_outbox[1] == {"user_id": "u1", "message": "echo:again"}

    async_session.expire_all()
    speaker_count = await async_session.execute(
        select(func.count()).select_from(Speaker)
    )
    conversation_count = await async_session.execute(
        select(func.count()).select_from(Conversation)
    )
    utterance_count = await async_session.execute(
        select(func.count()).select_from(Utterance)
    )

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


@pytest.mark.asyncio
async def test_chat_multiple_users_interleaved(
    async_client: AsyncClient,
    async_session: AsyncSession,
    sms_outbox: list[dict[str, str]],
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
        payload = {"user_id": user_id, "message": f"msg-{user_id}-{seen[user_id]}"}
        response = await async_client.post(
            "/chat",
            headers={"Authorization": "Bearer test-token"},
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

    assert len(sms_outbox) == sum(desired_counts.values())

    async_session.expire_all()
    speaker_count = await async_session.execute(
        select(func.count()).select_from(Speaker)
    )
    conversation_count = await async_session.execute(
        select(func.count()).select_from(Conversation)
    )
    utterance_count = await async_session.execute(
        select(func.count()).select_from(Utterance)
    )

    assert speaker_count.scalar_one() == 6
    assert conversation_count.scalar_one() == 3
    assert utterance_count.scalar_one() == sum(desired_counts.values()) * 2

    per_convo = await async_session.execute(
        select(Utterance.conversation_id, func.count())
        .group_by(Utterance.conversation_id)
    )
    counts_by_convo = {row[0]: row[1] for row in per_convo.all()}
    assert counts_by_convo == {
        conversation_ids["u1"]: 6,
        conversation_ids["u2"]: 10,
        conversation_ids["u3"]: 14,
    }


@pytest.mark.asyncio
async def test_chat_marks_failed_on_sms_error(
    async_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_send_sms(_: chat_service.SmsOutboundRequest) -> None:
        raise RuntimeError("sms gateway down")

    monkeypatch.setattr(chat_service, "send_sms", _fail_send_sms)

    payload = {"user_id": "u9", "message": "hello"}
    response = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
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
