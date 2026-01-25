import base64
import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.console import init_console
from app.db import get_engine


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(scope="module")
def admin_ui_env() -> None:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ADMIN_USERNAME", "admin")
    patcher.setenv("ADMIN_PASSWORD", "secret")
    patcher.setenv("ADMIN_SECRET_KEY", "test-admin-secret")
    database_url_test = os.getenv("DATABASE_URL_TEST")
    if not database_url_test:
        patcher.undo()
        pytest.skip("DATABASE_URL_TEST is not set.")
    patcher.setenv("DATABASE_URL", database_url_test)
    yield
    patcher.undo()


@pytest.fixture()
async def admin_ui_client(admin_ui_env: None) -> AsyncClient:
    get_engine.cache_clear()
    test_app = FastAPI()
    init_console(test_app)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    admin = getattr(test_app.state, "admin", None)
    if admin is not None and getattr(admin, "engine", None) is not None:
        await admin.engine.dispose()
    get_engine.cache_clear()


@pytest.mark.asyncio
async def test_admin_ui_redirects_without_auth(admin_ui_client: AsyncClient) -> None:
    response = await admin_ui_client.get("/console/admin/")
    assert response.status_code in (302, 307)
    assert "/console/admin/login" in response.headers.get("location", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/console/admin/speaker/list",
        "/console/admin/conversation/list",
        "/console/admin/utterance/list",
    ],
)
async def test_admin_ui_lists_with_basic_auth(admin_ui_client: AsyncClient, path: str) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await admin_ui_client.get(path, headers=headers)
    assert response.status_code == 200
