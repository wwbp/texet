from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_session
from app.main import app
from app.models import Conversation, Speaker, Utterance


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
    async_client: AsyncClient, async_session: AsyncSession
) -> None:
    payload = {"user_id": "u1", "message": "hello"}
    first = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["text"] == "echo:hello"
    assert len(first_body["conversation_id"]) == 32
    assert len(first_body["reply_utterance_id"]) == 32

    second_payload = {"user_id": "u1", "message": "again"}
    second = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=second_payload,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["text"] == "echo:again"
    assert second_body["conversation_id"] == first_body["conversation_id"]

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


@pytest.mark.asyncio
async def test_chat_multiple_users_interleaved(
    async_client: AsyncClient, async_session: AsyncSession
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
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == f"echo:{payload['message']}"

        if user_id not in conversation_ids:
            conversation_ids[user_id] = body["conversation_id"]
        else:
            assert body["conversation_id"] == conversation_ids[user_id]

    assert seen == desired_counts
    assert len(set(conversation_ids.values())) == 3

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
