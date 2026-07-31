import base64
import datetime
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_RECEIVED
from app.db import get_engine
from app.main import app
from app.response import service as response_service
from app.response.crud import (
    create_utterance,
    get_or_create_conversation,
    get_or_create_speaker,
    get_weekly_summary,
)
from app.response.utils import week_start_utc

_WEEK_START = datetime.date(2026, 4, 12)  # a Sunday
_MID_WEEK = datetime.date(2026, 4, 15)  # a Wednesday inside the same week
_WEEK_MID_DT = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)


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


@pytest.fixture()
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_generate_reply(_history: list[object], _query: str, _prompt: str) -> str:
        return "console-forced summary"

    monkeypatch.setattr(response_service, "_generate_reply", _fake_generate_reply)


async def _seed_message(session: AsyncSession, user_id: str) -> None:
    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        conversation = await get_or_create_conversation(session, speaker.id)
        utt = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            f"message from {user_id}",
            status=UTTERANCE_STATUS_RECEIVED,
        )
        utt.timestamp = _WEEK_MID_DT


@pytest.mark.asyncio
async def test_console_summaries_requires_auth(console_client: AsyncClient) -> None:
    assert (await console_client.get("/console/summaries")).status_code == 401
    assert (await console_client.post("/console/summaries", data={})).status_code == 401


@pytest.mark.asyncio
async def test_console_summaries_page_defaults_to_the_previous_week(
    console_client: AsyncClient,
) -> None:
    headers = _basic_auth_header("admin", "secret")

    page = await console_client.get("/console/summaries", headers=headers)

    assert page.status_code == 200
    previous_week = week_start_utc(datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=7)
    assert previous_week.isoformat() in page.text


@pytest.mark.asyncio
async def test_console_summaries_force_generates_for_all_users(
    console_client: AsyncClient,
    async_session: AsyncSession,
    stub_llm: None,
) -> None:
    headers = _basic_auth_header("admin", "secret")
    await _seed_message(async_session, "u-console-a")
    await _seed_message(async_session, "u-console-b")

    response = await console_client.post(
        "/console/summaries",
        headers=headers,
        data={"week_start": _WEEK_START.isoformat()},
    )

    assert response.status_code == 200
    assert "2 generated" in response.text
    assert await get_weekly_summary(async_session, "u-console-a", _WEEK_START) == (
        "console-forced summary"
    )
    assert await get_weekly_summary(async_session, "u-console-b", _WEEK_START) == (
        "console-forced summary"
    )


@pytest.mark.asyncio
async def test_console_summaries_normalizes_a_mid_week_date(
    console_client: AsyncClient,
    async_session: AsyncSession,
    stub_llm: None,
) -> None:
    """Summaries are keyed on the week's Sunday; a date picked mid-week must not
    create a second, off-grid row for the same week."""
    headers = _basic_auth_header("admin", "secret")
    await _seed_message(async_session, "u-console-mid")

    response = await console_client.post(
        "/console/summaries",
        headers=headers,
        data={"week_start": _MID_WEEK.isoformat()},
    )

    assert response.status_code == 200
    assert await get_weekly_summary(async_session, "u-console-mid", _WEEK_START) == (
        "console-forced summary"
    )
    assert await get_weekly_summary(async_session, "u-console-mid", _MID_WEEK) is None


@pytest.mark.asyncio
async def test_console_summaries_rejects_a_bad_date(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")

    response = await console_client.post(
        "/console/summaries",
        headers=headers,
        data={"week_start": "not-a-date"},
    )

    assert response.status_code == 400
    assert "Invalid week start date." in response.text


@pytest.mark.asyncio
async def test_console_summaries_lists_existing_summaries(
    console_client: AsyncClient,
    async_session: AsyncSession,
    stub_llm: None,
) -> None:
    headers = _basic_auth_header("admin", "secret")
    await _seed_message(async_session, "u-console-listed")

    await console_client.post(
        "/console/summaries",
        headers=headers,
        data={"week_start": _WEEK_START.isoformat()},
    )
    page = await console_client.get("/console/summaries", headers=headers)

    assert "u-console-listed" in page.text
    assert "console-forced summary" in page.text


@pytest.mark.asyncio
async def test_console_root_links_to_summaries(console_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")

    root = await console_client.get("/console", headers=headers)

    assert root.status_code == 200
    assert "/console/summaries" in root.text
