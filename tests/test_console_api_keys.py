import base64
import datetime
import html
import json
import os
import re

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_api_key
from app.auth.api_keys import create_api_key
from app.db import get_engine
from app.main import app
from app.models.auth import ApiKey


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
async def test_console_api_keys_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/api-keys")
    assert response.status_code == 401

    response = await console_client.post("/console/api-keys", data={"name": "test"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_api_keys_create(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/api-keys", headers=headers, data={"name": "console"}
    )
    assert response.status_code == 200

    match = re.search(r"texet_[A-Za-z0-9_-]+", response.text)
    assert match is not None
    key = match.group(0)

    async_session.expire_all()
    result = await async_session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
    api_key = result.scalar_one_or_none()
    assert api_key is not None
    assert api_key.name == "console"
    assert api_key.key_prefix == key[:8]
    assert api_key.is_active is True


ADMIN = _basic_auth_header("admin", "secret")
LAST_USED = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


async def _seed_key(
    session: AsyncSession,
    name: str,
    scope: str = "response",
    last_used_at: datetime.datetime | None = None,
) -> tuple[str, dict[str, str]]:
    async with session.begin():
        token = await create_api_key(session, name=name, scope=scope)
        result = await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
        row = result.scalar_one()
        row.last_used_at = last_used_at
    return row.id, {"Authorization": f"Bearer {token}"}


async def _row(session: AsyncSession, key_id: str) -> ApiKey | None:
    session.expire_all()
    return await session.get(ApiKey, key_id)


def _table_row(page: str, key_id: str) -> str:
    return next(row for row in page.split("<tr>") if f"<td>{key_id}</td>" in row)


@pytest.mark.asyncio
async def test_revoke_requires_admin_auth(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, _ = await _seed_key(async_session, "hub")
    response = await console_client.post(f"/console/api-keys/{key_id}/revoke")
    assert response.status_code == 401
    row = await _row(async_session, key_id)
    assert row is not None and row.is_active is True


@pytest.mark.asyncio
async def test_revoke_deactivates_and_keeps_row(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, _ = await _seed_key(async_session, "hub")
    response = await console_client.post(f"/console/api-keys/{key_id}/revoke", headers=ADMIN)
    assert response.status_code == 200
    row = await _row(async_session, key_id)
    assert row is not None and row.is_active is False


@pytest.mark.asyncio
async def test_revoke_leaves_last_used_at_unchanged(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    # An old timestamp, so the 60s refresh throttle in require_scope cannot hide a write.
    key_id, headers = await _seed_key(async_session, "old", "export", LAST_USED)
    await console_client.post(f"/console/api-keys/{key_id}/revoke", headers=ADMIN)
    assert (await console_client.get("/exports/convokit", headers=headers)).status_code == 401
    row = await _row(async_session, key_id)
    assert row is not None and row.last_used_at == LAST_USED


@pytest.mark.asyncio
async def test_revoked_export_key_gets_401_other_key_works(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, revoked = await _seed_key(async_session, "lost", "export")
    _, active = await _seed_key(async_session, "kept", "export")
    assert (await console_client.get("/exports/convokit", headers=revoked)).status_code == 200

    await console_client.post(f"/console/api-keys/{key_id}/revoke", headers=ADMIN)

    assert (await console_client.get("/exports/convokit", headers=revoked)).status_code == 401
    assert (await console_client.get("/exports/convokit", headers=active)).status_code == 200


@pytest.mark.asyncio
async def test_revoked_response_key_gets_401_other_key_works(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, revoked = await _seed_key(async_session, "lost")
    _, active = await _seed_key(async_session, "kept")

    # An empty body: 422 means auth passed, without running the reply pipeline.
    assert (await console_client.post("/response", json={}, headers=revoked)).status_code == 422
    await console_client.post(f"/console/api-keys/{key_id}/revoke", headers=ADMIN)

    assert (await console_client.post("/response", json={}, headers=revoked)).status_code == 401
    assert (await console_client.post("/response", json={}, headers=active)).status_code == 422


@pytest.mark.asyncio
async def test_revoke_twice_is_safe(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, _ = await _seed_key(async_session, "hub", last_used_at=LAST_USED)
    for _attempt in range(2):
        response = await console_client.post(f"/console/api-keys/{key_id}/revoke", headers=ADMIN)
        assert response.status_code == 200
    row = await _row(async_session, key_id)
    assert row is not None and row.is_active is False and row.last_used_at == LAST_USED


@pytest.mark.asyncio
async def test_revoke_unknown_id_is_not_found(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    key_id, _ = await _seed_key(async_session, "hub")
    response = await console_client.post("/console/api-keys/nope/revoke", headers=ADMIN)
    assert response.status_code == 404
    assert "API key not found." in response.text
    row = await _row(async_session, key_id)
    assert row is not None and row.is_active is True


@pytest.mark.asyncio
async def test_revoke_button_only_on_active_rows(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    active_id, _ = await _seed_key(async_session, "kept")
    revoked_id, _ = await _seed_key(async_session, "lost", "export")
    await console_client.post(f"/console/api-keys/{revoked_id}/revoke", headers=ADMIN)

    page = (await console_client.get("/console/api-keys", headers=ADMIN)).text

    active_row = _table_row(page, active_id)
    assert f'action="/console/api-keys/{active_id}/revoke"' in active_row
    assert ">Revoke</button>" in active_row
    assert "<td>response</td>" in active_row

    revoked_row = _table_row(page, revoked_id)
    assert "revoke" not in revoked_row.lower()
    assert "<td>export</td>" in revoked_row


@pytest.mark.asyncio
async def test_revoke_confirm_names_key_and_cannot_break_out(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    name = """x");alert('1');//<b>"""
    key_id, _ = await _seed_key(async_session, name)

    page = (await console_client.get("/console/api-keys", headers=ADMIN)).text
    row = _table_row(page, key_id)
    assert "<b>" not in row

    # The browser unescapes the attribute, then runs it. The argument must still be
    # one JS string literal (JSON strings are valid JS), holding the name verbatim.
    match = re.search(r'onsubmit="([^"]*)"', row)
    assert match is not None
    script = html.unescape(match.group(1))
    assert script.startswith("return confirm(") and script.endswith(")")
    assert name in json.loads(script[len("return confirm(") : -1])


@pytest.mark.asyncio
async def test_empty_table_colspan_matches_columns(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    page = (await console_client.get("/console/api-keys", headers=ADMIN)).text
    match = re.search(r'colspan="(\d+)"', page)
    assert match is not None
    assert int(match.group(1)) == page.count("<th>")
