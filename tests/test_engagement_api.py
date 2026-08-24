"""GET /engagement and the console page that renders the same rows."""

from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_api_key
from app.config import UTTERANCE_STATUS_SENT
from app.db import get_async_session
from app.main import app
from app.models.auth import ApiKey
from app.response.crud import (
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)

API_KEY = "test-api-key"


@pytest.fixture()
async def client(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    async with async_session.begin():
        async_session.add(
            ApiKey(
                name="engagement-key",
                key_hash=hash_api_key(API_KEY),
                key_prefix=API_KEY[:8],
                is_active=True,
            )
        )

    app.dependency_overrides[get_async_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed_day(
    session: AsyncSession,
    user_id: str,
    when: datetime.datetime,
    *,
    participant_messages: int = 0,
    tokens: dict | None = None,
) -> None:
    speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
    bot = await get_or_create_bot_speaker(session, user_id)
    conversation = await get_or_create_conversation(session, speaker.id)
    await session.commit()

    ping = await create_utterance(
        session,
        conversation.id,
        bot.id,
        "Good morning!",
        meta={"texet_hub_initial": True},
        status=UTTERANCE_STATUS_SENT,
    )
    ping.timestamp = when
    if tokens:
        reply = await create_utterance(
            session,
            conversation.id,
            bot.id,
            "reply",
            meta={"texet_usage": tokens},
            status=UTTERANCE_STATUS_SENT,
        )
        reply.timestamp = when
    for i in range(participant_messages):
        msg = await create_utterance(session, conversation.id, speaker.id, f"m{i}")
        msg.timestamp = when + datetime.timedelta(minutes=i + 1)
    await session.commit()


@pytest.mark.asyncio
async def test_engagement_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/engagement")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_engagement_returns_a_row_per_participant_day(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    when = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _seed_day(
        async_session,
        "u-api-1",
        when,
        participant_messages=3,
        tokens={"prompt_tokens": 100, "completion_tokens": 25},
    )

    response = await client.get("/engagement", headers={"Authorization": f"Bearer {API_KEY}"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "participant_id": "u-api-1",
            "date": "2026-04-14",
            "engaged": True,
            "utterance_count": 3,
            "token_count": 125,
        }
    ]


@pytest.mark.asyncio
async def test_unmeasured_tokens_serialize_as_null_not_zero(
    client: AsyncClient, async_session: AsyncSession
) -> None:
    when = datetime.datetime(2026, 4, 14, 12, 0, tzinfo=datetime.UTC)
    await _seed_day(async_session, "u-api-2", when, participant_messages=1)

    response = await client.get("/engagement", headers={"Authorization": f"Bearer {API_KEY}"})

    assert response.json()[0]["token_count"] is None


@pytest.mark.asyncio
async def test_date_range_is_honoured(client: AsyncClient, async_session: AsyncSession) -> None:
    await _seed_day(
        async_session, "u-api-3", datetime.datetime(2026, 4, 14, 12, tzinfo=datetime.UTC)
    )
    await _seed_day(
        async_session, "u-api-3", datetime.datetime(2026, 4, 20, 12, tzinfo=datetime.UTC)
    )

    response = await client.get(
        "/engagement?start=2026-04-19&end=2026-04-21",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )

    assert [row["date"] for row in response.json()] == ["2026-04-20"]


@pytest.mark.asyncio
async def test_start_after_end_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        "/engagement?start=2026-04-20&end=2026-04-14",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_malformed_date_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        "/engagement?start=not-a-date", headers={"Authorization": f"Bearer {API_KEY}"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Console page
# ---------------------------------------------------------------------------


@pytest.fixture()
async def console_client(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "console-secret")

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", auth=("admin", "secret")
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_console_page_renders_rows(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    today = datetime.datetime.now(datetime.UTC)
    await _seed_day(
        async_session,
        "u-console-1",
        today,
        participant_messages=2,
        tokens={"prompt_tokens": 90, "completion_tokens": 10},
    )

    response = await console_client.get("/console/engagement")

    assert response.status_code == 200
    body = response.text
    assert "u-console-1" in body
    assert ">yes<" in body
    assert "100" in body


@pytest.mark.asyncio
async def test_console_page_shows_dash_for_unmeasured_tokens(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    """Zero would read as a free day; the column has to say 'unknown'."""
    await _seed_day(
        async_session, "u-console-2", datetime.datetime.now(datetime.UTC), participant_messages=1
    )

    response = await console_client.get("/console/engagement")

    assert "—" in response.text


@pytest.mark.asyncio
async def test_console_page_rejects_a_backwards_range(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/engagement?start=2026-04-20&end=2026-04-14")
    assert response.status_code == 200
    assert "Start must not be after end." in response.text


@pytest.mark.asyncio
async def test_console_page_rejects_a_malformed_date(console_client: AsyncClient) -> None:
    response = await console_client.get("/console/engagement?start=nope")
    assert response.status_code == 200
    assert "Dates must be YYYY-MM-DD." in response.text


@pytest.mark.asyncio
async def test_console_page_requires_admin(async_session: AsyncSession) -> None:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        response = await anon.get("/console/engagement")
    app.dependency_overrides.clear()

    assert response.status_code in (401, 403)
