import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.main import app
from app.models.response import SystemPrompt


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def console_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-admin-secret")
    database_url_test = os.getenv("DATABASE_URL_TEST")
    if not database_url_test:
        pytest.skip("DATABASE_URL_TEST is not set.")
    monkeypatch.setenv("DATABASE_URL", database_url_test)
    get_engine.cache_clear()


@pytest.fixture()
async def console_client(console_env: None) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_console_system_prompts_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/system-prompts")
    assert response.status_code == 401

    response = await console_client.post("/console/system-prompts", data={"prompt": "x"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_system_prompts_crud(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/system-prompts", headers=headers)
    assert index.status_code == 200
    assert "Latest created prompt (top row) is used by the system." in index.text

    create = await console_client.post(
        "/console/system-prompts",
        headers=headers,
        data={"prompt": "be concise"},
    )
    assert create.status_code == 200

    result = await async_session.execute(
        select(SystemPrompt).order_by(SystemPrompt.created_at.desc()).limit(1)
    )
    created = result.scalar_one()
    assert created.prompt == "be concise"
    created_id = created.id

    update = await console_client.post(
        f"/console/system-prompts/{created_id}",
        headers=headers,
        data={"prompt": "be concise and clear"},
    )
    assert update.status_code == 200

    async_session.expire_all()
    updated = await async_session.get(SystemPrompt, created_id)
    assert updated is not None
    assert updated.prompt == "be concise and clear"

    delete = await console_client.post(
        f"/console/system-prompts/{created_id}/delete",
        headers=headers,
    )
    assert delete.status_code == 200

    async_session.expire_all()
    check = await async_session.get(SystemPrompt, created_id)
    assert check is None


@pytest.mark.asyncio
async def test_console_system_prompts_errors(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    empty = await console_client.post(
        "/console/system-prompts",
        headers=headers,
        data={"prompt": "   "},
    )
    assert empty.status_code == 400
    assert "Prompt is required." in empty.text

    missing = await console_client.post(
        "/console/system-prompts/missing-id",
        headers=headers,
        data={"prompt": "new"},
    )
    assert missing.status_code == 400
    assert "Prompt not found." in missing.text
