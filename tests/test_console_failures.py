import base64
import datetime
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_FAILED
from app.db import get_engine
from app.main import app
from app.models.response import PromptIssue
from app.response.crud import (
    create_queued_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)


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


async def _failed_reply(session: AsyncSession, user_id: str, error: str) -> str:
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, user_id)
        conversation = await get_or_create_conversation(session, speaker.id)
        bot_utt = await create_queued_utterance(session, conversation.id, bot.id)
        bot_utt.status = UTTERANCE_STATUS_FAILED
        bot_utt.error = error
        bot_utt.attempts = 3
        bot_utt.timestamp = datetime.datetime.now(datetime.UTC)
        utt_id = bot_utt.id
    return utt_id


@pytest.mark.asyncio
async def test_requires_auth(console_client: AsyncClient) -> None:
    assert (await console_client.get("/console/failures")).status_code == 401


@pytest.mark.asyncio
async def test_shows_failed_reply_with_its_error(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    await _failed_reply(async_session, "u-fail-console", "bedrock ThrottlingException")

    page = await console_client.get(
        "/console/failures", headers=_basic_auth_header("admin", "secret")
    )
    assert page.status_code == 200
    assert "u-fail-console" in page.text
    assert "bedrock ThrottlingException" in page.text


@pytest.mark.asyncio
async def test_shows_prompt_issues(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    async with async_session.begin():
        async_session.add(
            PromptIssue(
                kind="day_number_invalid",
                user_id="u-issue-console",
                utterance_id="abc123",
                detail="day_number arrived as the string '7'; coerced to 7.",
            )
        )

    page = await console_client.get(
        "/console/failures", headers=_basic_auth_header("admin", "secret")
    )
    assert page.status_code == 200
    assert "day_number_invalid" in page.text
    assert "u-issue-console" in page.text
    assert "coerced to 7" in page.text


@pytest.mark.asyncio
async def test_escapes_error_text(console_client: AsyncClient, async_session: AsyncSession) -> None:
    """Error strings come from provider exceptions; they must not render as markup."""
    await _failed_reply(async_session, "u-fail-xss", "<script>alert(1)</script>")

    page = await console_client.get(
        "/console/failures", headers=_basic_auth_header("admin", "secret")
    )
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


@pytest.mark.asyncio
async def test_clean_system_says_so(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    page = await console_client.get(
        "/console/failures", headers=_basic_auth_header("admin", "secret")
    )
    assert page.status_code == 200
    assert "No failed replies" in page.text
    assert "No prompt issues" in page.text


@pytest.mark.asyncio
async def test_console_root_links_to_failures(console_client: AsyncClient) -> None:
    root = await console_client.get("/console", headers=_basic_auth_header("admin", "secret"))
    assert root.status_code == 200
    assert "/console/failures" in root.text
