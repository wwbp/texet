import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.main import app
from app.models.response import SummarizationPrompt


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
async def test_console_summarization_prompts_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/summarization-prompts")
    assert response.status_code == 401

    response = await console_client.post("/console/summarization-prompts", data={"prompt": "x"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_summarization_prompts_crud(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/summarization-prompts", headers=headers)
    assert index.status_code == 200
    assert "Latest created prompt (top row) is used by the system." in index.text

    create = await console_client.post(
        "/console/summarization-prompts",
        headers=headers,
        data={"prompt": "summarize tersely"},
    )
    assert create.status_code == 200

    result = await async_session.execute(
        select(SummarizationPrompt).order_by(SummarizationPrompt.created_at.desc()).limit(1)
    )
    created = result.scalar_one()
    assert created.prompt == "summarize tersely"
    created_id = created.id

    update = await console_client.post(
        f"/console/summarization-prompts/{created_id}",
        headers=headers,
        data={"prompt": "summarize tersely and clearly"},
    )
    assert update.status_code == 200

    async_session.expire_all()
    updated = await async_session.get(SummarizationPrompt, created_id)
    assert updated is not None
    assert updated.prompt == "summarize tersely and clearly"

    delete = await console_client.post(
        f"/console/summarization-prompts/{created_id}/delete",
        headers=headers,
    )
    assert delete.status_code == 200

    async_session.expire_all()
    check = await async_session.get(SummarizationPrompt, created_id)
    assert check is None


@pytest.mark.asyncio
async def test_console_summarization_prompts_shows_default_when_empty(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/summarization-prompts", headers=headers)
    assert index.status_code == 200
    assert "No summarization prompts yet" in index.text
    assert "built-in default" in index.text


@pytest.mark.asyncio
async def test_console_root_links_to_summarization_prompts(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")

    root = await console_client.get("/console", headers=headers)
    assert root.status_code == 200
    assert "/console/summarization-prompts" in root.text


@pytest.mark.asyncio
async def test_console_summarization_prompts_errors(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    empty = await console_client.post(
        "/console/summarization-prompts",
        headers=headers,
        data={"prompt": "   "},
    )
    assert empty.status_code == 400
    assert "Prompt is required." in empty.text

    missing = await console_client.post(
        "/console/summarization-prompts/missing-id",
        headers=headers,
        data={"prompt": "new"},
    )
    assert missing.status_code == 400
    assert "Prompt not found." in missing.text
