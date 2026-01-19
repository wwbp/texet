import base64
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_RECEIVED
from app.db import get_async_session
from app.db_ops import create_utterance, get_or_create_conversation, get_or_create_speaker
from app.main import app


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-admin-secret")


@pytest.fixture()
async def admin_client(
    async_session: AsyncSession, admin_env: None
) -> AsyncClient:
    async def _override_dependency() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_export_requires_auth(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/admin/export/utterances")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_export_utterances_csv(
    admin_client: AsyncClient, async_session: AsyncSession
) -> None:
    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u1", meta={"type": "user"}
        )
        conversation = await get_or_create_conversation(async_session, speaker.id)
        await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "hello",
            status=UTTERANCE_STATUS_RECEIVED,
        )

    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get("/admin/export/utterances", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "hello" in response.text


@pytest.mark.asyncio
async def test_admin_export_utterances_json(
    admin_client: AsyncClient, async_session: AsyncSession
) -> None:
    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u2", meta={"type": "user"}
        )
        conversation = await get_or_create_conversation(async_session, speaker.id)
        await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "json-msg",
            status=UTTERANCE_STATUS_RECEIVED,
        )

    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get(
        "/admin/export/utterances?format=json", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert any(row["text"] == "json-msg" for row in payload)


@pytest.mark.asyncio
async def test_admin_export_utterances_include_meta(
    admin_client: AsyncClient, async_session: AsyncSession
) -> None:
    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-meta", meta={"type": "user"}
        )
        conversation = await get_or_create_conversation(async_session, speaker.id)
        await create_utterance(
            async_session,
            conversation.id,
            speaker.id,
            "meta-msg",
            status=UTTERANCE_STATUS_RECEIVED,
            meta={"source": "test"},
        )

    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get(
        "/admin/export/utterances?format=json&include_meta=true", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert any(row["meta"] == {"source": "test"} for row in payload)


@pytest.mark.asyncio
async def test_admin_export_invalid_status(admin_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get(
        "/admin/export/utterances?status=not-a-status", headers=headers
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_admin_export_invalid_since(admin_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get(
        "/admin/export/utterances?since=not-a-date", headers=headers
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_admin_export_conversations_csv(
    admin_client: AsyncClient, async_session: AsyncSession
) -> None:
    async with async_session.begin():
        speaker = await get_or_create_speaker(
            async_session, "u-conv", meta={"type": "user"}
        )
        conversation = await get_or_create_conversation(async_session, speaker.id)

    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get("/admin/export/conversations", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert conversation.id in response.text
