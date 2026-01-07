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
    response = await async_client.post("/chat", json={"user_id": "u1", "message": "hello"})
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
async def test_chat_success_persists(async_client: AsyncClient, async_session: AsyncSession) -> None:
    payload = {"user_id": "u1", "message": "hello"}
    response = await async_client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "echo:hello"
    assert len(body["conversation_id"]) == 32
    assert len(body["reply_utterance_id"]) == 32

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
    assert utterance_count.scalar_one() == 2
