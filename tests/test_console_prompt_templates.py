import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.main import app
from app.models.response import InstructionTemplate


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
async def test_console_prompt_templates_requires_auth(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/prompt-templates")
    assert response.status_code == 401

    response = await console_client.post("/console/prompt-templates", data={"template": "{base}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_console_prompt_templates_crud(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/prompt-templates", headers=headers)
    assert index.status_code == 200
    assert "Latest created template (top row) is used by the system." in index.text

    create = await console_client.post(
        "/console/prompt-templates",
        headers=headers,
        data={"template": "{base}\n\n{weekly_summary}"},
    )
    assert create.status_code == 200

    result = await async_session.execute(
        select(InstructionTemplate).order_by(InstructionTemplate.created_at.desc()).limit(1)
    )
    created = result.scalar_one()
    assert created.template == "{base}\n\n{weekly_summary}"
    created_id = created.id

    update = await console_client.post(
        f"/console/prompt-templates/{created_id}",
        headers=headers,
        data={"template": "{base}\n\n{daily_content}"},
    )
    assert update.status_code == 200

    async_session.expire_all()
    updated = await async_session.get(InstructionTemplate, created_id)
    assert updated is not None
    assert updated.template == "{base}\n\n{daily_content}"

    delete = await console_client.post(
        f"/console/prompt-templates/{created_id}/delete",
        headers=headers,
    )
    assert delete.status_code == 200

    async_session.expire_all()
    assert await async_session.get(InstructionTemplate, created_id) is None


@pytest.mark.asyncio
async def test_console_prompt_templates_documents_placeholders(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/prompt-templates", headers=headers)
    assert index.status_code == 200
    for placeholder in ("{base}", "{day_suffix}", "{daily_content}", "{weekly_summary}"):
        assert placeholder in index.text
    # The paragraph-drop rule is the surprising part; it must be spelled out.
    assert "paragraph" in index.text.lower()


@pytest.mark.asyncio
async def test_console_prompt_templates_shows_default_when_empty(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    index = await console_client.get("/console/prompt-templates", headers=headers)
    assert index.status_code == 200
    assert "No templates yet" in index.text
    assert "built-in default" in index.text


@pytest.mark.asyncio
async def test_console_root_links_to_prompt_templates(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")

    root = await console_client.get("/console", headers=headers)
    assert root.status_code == 200
    assert "/console/prompt-templates" in root.text


@pytest.mark.asyncio
async def test_console_prompt_templates_errors(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")

    empty = await console_client.post(
        "/console/prompt-templates",
        headers=headers,
        data={"template": "   "},
    )
    assert empty.status_code == 400
    assert "Template is required." in empty.text

    missing = await console_client.post(
        "/console/prompt-templates/missing-id",
        headers=headers,
        data={"template": "{base}"},
    )
    assert missing.status_code == 400
    assert "Template not found." in missing.text


@pytest.mark.asyncio
async def test_console_prompt_templates_rejects_template_without_base(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    response = await console_client.post(
        "/console/prompt-templates",
        headers=headers,
        data={"template": "No placeholder here."},
    )
    assert response.status_code == 400
    assert "{base}" in response.text

    result = await async_session.execute(select(InstructionTemplate))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_console_prompt_templates_update_rejects_template_without_base(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    async with async_session.begin():
        template = InstructionTemplate(template="{base}")
        async_session.add(template)
    template_id = template.id

    response = await console_client.post(
        f"/console/prompt-templates/{template_id}",
        headers=headers,
        data={"template": "dropped the placeholder"},
    )
    assert response.status_code == 400

    async_session.expire_all()
    unchanged = await async_session.get(InstructionTemplate, template_id)
    assert unchanged is not None
    assert unchanged.template == "{base}"
