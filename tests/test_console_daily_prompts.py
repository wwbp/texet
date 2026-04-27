from __future__ import annotations

import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.main import app
from app.models.response import DailyPrompt


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
async def test_console_daily_prompts_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/daily-prompts")
    assert response.status_code == 401

    response = await console_client.post(
        "/console/daily-prompts", data={"day_identifier": "1", "content": "x"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_daily_prompts_list_empty(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.get("/console/daily-prompts", headers=headers)
    assert response.status_code == 200
    assert "No daily prompts yet." in response.text


@pytest.mark.asyncio
async def test_console_daily_prompts_crud(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    create = await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"day_identifier": "3", "content": "Walk 10 minutes."},
    )
    assert create.status_code == 200
    assert "Walk 10 minutes." in create.text

    result = await async_session.execute(
        select(DailyPrompt).where(DailyPrompt.day_identifier == 3)
    )
    created = result.scalar_one()
    assert created.content == "Walk 10 minutes."
    prompt_id = created.id

    update = await console_client.post(
        f"/console/daily-prompts/{prompt_id}",
        headers=headers,
        data={"content": "Walk 20 minutes."},
    )
    assert update.status_code == 200

    async_session.expire_all()
    updated = await async_session.get(DailyPrompt, prompt_id)
    assert updated is not None
    assert updated.content == "Walk 20 minutes."

    delete = await console_client.post(
        f"/console/daily-prompts/{prompt_id}/delete",
        headers=headers,
    )
    assert delete.status_code == 200

    async_session.expire_all()
    check = await async_session.get(DailyPrompt, prompt_id)
    assert check is None


@pytest.mark.asyncio
async def test_console_daily_prompts_duplicate_identifier(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"day_identifier": "10", "content": "First."},
    )
    response = await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"day_identifier": "10", "content": "Duplicate."},
    )
    assert response.status_code == 400
    assert "already exists" in response.text


@pytest.mark.asyncio
async def test_console_daily_prompts_missing_content(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"day_identifier": "2", "content": "   "},
    )
    assert response.status_code == 400
    assert "Content is required." in response.text


@pytest.mark.asyncio
async def test_console_daily_prompts_missing_day_identifier(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"content": "Some content."},
    )
    assert response.status_code == 400
    assert "Day number is required." in response.text


@pytest.mark.asyncio
async def test_console_daily_prompts_invalid_day_identifier(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/daily-prompts",
        headers=headers,
        data={"day_identifier": "0", "content": "Some content."},
    )
    assert response.status_code == 400
    assert "positive integer" in response.text


@pytest.mark.asyncio
async def test_console_daily_prompts_update_not_found(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/daily-prompts/nonexistent-id",
        headers=headers,
        data={"content": "New content."},
    )
    assert response.status_code == 400
    assert "Prompt not found." in response.text
