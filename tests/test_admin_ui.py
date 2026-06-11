import base64
import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from markupsafe import Markup
from sqladmin.filters import OperationColumnFilter

from app.console import init_console
from app.console.admin_ui import UtteranceAdmin, _fmt_meta_detail
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


def test_utterance_admin_has_user_filter_and_timestamp_default_sort() -> None:
    assert UtteranceAdmin.column_default_sort == [("timestamp", True), ("id", True)]
    assert UtteranceAdmin.column_labels["speaker_id"] == "User"
    assert any(
        isinstance(filter_config, OperationColumnFilter)
        and getattr(filter_config, "parameter_name", None) == "speaker_id"
        for filter_config in UtteranceAdmin.column_filters
    )


def test_utterance_admin_detail_renders_collapsible_snapshot() -> None:
    class Row:
        meta = {
            "texet_generation": {
                "provider": "bedrock",
                "model_id": "us.anthropic.claude-sonnet-4-6",
                "query": "What should I do next?",
                "chat_history": [{"role": "user", "content": "hello"}],
            },
            "texet_moderation_score": 0.42,
        }

    rendered = _fmt_meta_detail(Row(), "meta")

    # Dict values become a collapsible json-viewer component fed escaped JSON.
    assert isinstance(rendered, Markup)
    assert "/console/static/json_viewer.js" in rendered
    assert "<json-viewer data=" in rendered
    assert "Generation Snapshot" in rendered
    assert "&#34;provider&#34;: &#34;bedrock&#34;" in rendered
    # Scalar values stay as plain labelled lines.
    assert "Moderation Score:</strong> 0.42" in rendered


def test_meta_formatter_escapes_user_content() -> None:
    class Row:
        meta = {"texet_generation": {"query": '<script>alert("xss")</script>'}}

    rendered = _fmt_meta_detail(Row(), "meta")

    assert "<script>alert" not in rendered
    assert "json_viewer.js" in rendered  # the only script is the vendored viewer


def test_meta_formatter_empty_meta() -> None:
    class Row:
        meta = None

    assert _fmt_meta_detail(Row(), "meta") == "—"


@pytest.mark.asyncio
async def test_console_static_serves_json_viewer(admin_ui_client: AsyncClient) -> None:
    response = await admin_ui_client.get("/console/static/json_viewer.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
