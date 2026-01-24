import base64
import os
import re

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_api_key
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
    result = await async_session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
    )
    api_key = result.scalar_one_or_none()
    assert api_key is not None
    assert api_key.name == "console"
    assert api_key.key_prefix == key[:8]
    assert api_key.is_active is True
