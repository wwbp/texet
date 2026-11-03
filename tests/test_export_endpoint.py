"""GET /exports/convokit: export-scope keys only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.auth.api_keys import create_api_key
from app.db import get_async_session
from app.main import app
from app.models.admin import AdminExport
from app.models.auth import ApiKey
from app.models.response import Conversation, Speaker, Utterance


@pytest.fixture()
async def client(async_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _key(session: AsyncSession, scope: str) -> dict[str, str]:
    async with session.begin():
        key = await create_api_key(session, name=scope, scope=scope)
    return {"Authorization": f"Bearer {key}"}


async def _seed(session: AsyncSession) -> None:
    session.add_all([Speaker(id="u1"), Speaker(id="bot:u1")])
    await session.commit()
    session.add(Conversation(id="c1", owner_speaker_id="u1"))
    await session.commit()
    session.add(Utterance(id="x1", conversation_id="c1", speaker_id="u1", text="hi"))
    await session.commit()


@pytest.mark.asyncio
async def test_missing_or_bad_key_is_401(client: AsyncClient) -> None:
    assert (await client.get("/exports/convokit")).status_code == 401
    bad = {"Authorization": "Bearer texet_nope"}
    assert (await client.get("/exports/convokit", headers=bad)).status_code == 401


@pytest.mark.asyncio
async def test_response_key_cannot_export(client: AsyncClient, async_session: AsyncSession) -> None:
    headers = await _key(async_session, "response")
    assert (await client.get("/exports/convokit", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_export_key_cannot_post_responses(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    headers = await _key(async_session, "export")
    payload = {"user_id": "u1", "input": "hi", "mode": "text"}
    assert (await client.post("/response", json=payload, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_export_key_gets_zip_with_headers(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    await _seed(async_session)
    headers = await _key(async_session, "export")
    response = await client.get(
        "/exports/convokit",
        params={"include_generation_snapshot": "true"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["x-texet-export-version"] == "2"
    assert response.headers["x-texet-export-sha256"] == hashlib.sha256(response.content).hexdigest()

    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert any(n.endswith("/texet_config.json") for n in names)

    export = await async_session.get(AdminExport, response.headers["x-texet-export-id"])
    assert export is not None
    assert export.status == "completed"
    assert export.sha256 == response.headers["x-texet-export-sha256"]
    assert export.meta is not None
    assert export.meta["source"] == "api"
    assert export.meta["include_generation_snapshot"] is True
    key = (await async_session.execute(select(ApiKey).where(ApiKey.scope == "export"))).scalar_one()
    assert export.meta["api_key_id"] == key.id


@pytest.mark.asyncio
async def test_since_after_until_is_400(client: AsyncClient, async_session: AsyncSession) -> None:
    headers = await _key(async_session, "export")
    params = {"since": "2026-03-02T00:00:00", "until": "2026-03-01T00:00:00"}
    response = await client.get("/exports/convokit", params=params, headers=headers)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_console_creates_export_key(
    client: AsyncClient, async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    page = await client.post(
        "/console/api-keys", data={"name": "ckviz", "scope": "export"}, headers=auth
    )
    assert page.status_code == 200
    bad = await client.post("/console/api-keys", data={"scope": "admin"}, headers=auth)
    assert bad.status_code == 400
    match = re.search(r"texet_[A-Za-z0-9_-]+", page.text)
    assert match is not None
    key = (await async_session.execute(select(ApiKey).where(ApiKey.name == "ckviz"))).scalar_one()
    assert key.scope == "export"


def test_scope_migration_up_and_down() -> None:
    url = os.environ["DATABASE_URL_TEST"]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    os.environ["DATABASE_URL"] = url

    async def _sql(statement: str) -> object:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            result = await conn.execute(text(statement))
            value = result.scalar() if result.returns_rows else None
        await engine.dispose()
        return value

    asyncio.run(_sql("TRUNCATE api_keys"))
    command.downgrade(config, "50f6b01aaf1e")  # the revision before the scope column
    asyncio.run(
        _sql(
            "INSERT INTO api_keys (id, key_hash, key_prefix, is_active, created_at) "
            "VALUES ('old', 'h', 'p', true, now())"
        )
    )
    command.upgrade(config, "head")
    # Existing keys keep the response scope.
    assert asyncio.run(_sql("SELECT scope FROM api_keys WHERE id = 'old'")) == "response"
    asyncio.run(_sql("TRUNCATE api_keys"))
