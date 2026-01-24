import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


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
async def test_console_docs_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/docs")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_docs_with_basic_auth(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await console_client.get("/console/docs", headers=headers)
    assert response.status_code == 200
    assert "SwaggerUIBundle" in response.text


@pytest.mark.asyncio
async def test_console_openapi_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/openapi.json")
    assert response.status_code == 401

    headers = _basic_auth_header("admin", "secret")
    response = await console_client.get("/console/openapi.json", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert "paths" in payload
    assert set(payload["paths"].keys()) == {"/response", "/health", "/db/health"}
