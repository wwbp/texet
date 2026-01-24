import base64
import io
import json
import os
import zipfile
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMEZONE
from app.db import get_engine
from app.main import app
from app.models.admin import AdminExport
from app.models.response import Conversation, Speaker, Utterance


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
async def test_console_exports_convokit(
    console_client: AsyncClient,
    async_session: AsyncSession,
) -> None:
    now = datetime(2026, 1, 23, 12, 0, 0, tzinfo=DEFAULT_TIMEZONE)
    later = now + timedelta(seconds=5)

    speaker = Speaker(id="u1", meta={"role": "user"})
    bot = Speaker(id="bot:u1", meta={"type": "bot"})
    conversation = Conversation(
        id="c1",
        owner_speaker_id="u1",
        status="open",
        last_activity_at=later,
        meta={"topic": "demo"},
    )
    utterance = Utterance(
        id="u1-1",
        conversation_id="c1",
        speaker_id="u1",
        reply_to_id=None,
        timestamp=now,
        status="received",
        text="hello",
        error=None,
        meta={"source": "sms"},
    )
    bot_utterance = Utterance(
        id="u1-2",
        conversation_id="c1",
        speaker_id="bot:u1",
        reply_to_id="u1-1",
        timestamp=later,
        status="sent",
        text="hi",
        error=None,
        meta=None,
    )

    async_session.add_all([speaker, bot])
    await async_session.commit()
    async_session.add_all([conversation, utterance, bot_utterance])
    await async_session.commit()

    headers = _basic_auth_header("admin", "secret")
    response = await console_client.post(
        "/console/exports",
        headers=headers,
        data={"since": now.isoformat(), "until": later.isoformat()},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert names
    root = names[0].split("/")[0]
    required = {
        f"{root}/utterances.jsonl",
        f"{root}/speakers.json",
        f"{root}/conversations.json",
        f"{root}/corpus.json",
        f"{root}/index.json",
    }
    assert required.issubset(set(names))

    utterance_lines = archive.read(f"{root}/utterances.jsonl").decode().splitlines()
    utterance_rows = [json.loads(line) for line in utterance_lines if line.strip()]
    assert len(utterance_rows) == 2
    assert {row["speaker"] for row in utterance_rows} == {"u1", "bot:u1"}
    assert {row["conversation_id"] for row in utterance_rows} == {"u1-1"}
    for row in utterance_rows:
        assert "meta" in row
        assert "texet_status" in row["meta"]

    speakers = json.loads(archive.read(f"{root}/speakers.json"))
    conversations = json.loads(archive.read(f"{root}/conversations.json"))
    assert len(speakers) == 2
    assert len(conversations) == 1
    assert list(conversations.keys()) == ["u1-1"]
    assert conversations["u1-1"]["texet_conversation_id"] == "c1"

    index_payload = json.loads(archive.read(f"{root}/index.json"))
    assert "utterances-index" in index_payload
    assert "speakers-index" in index_payload
    assert "conversations-index" in index_payload
    assert "overall-index" in index_payload

    export_result = await async_session.execute(
        select(AdminExport).order_by(AdminExport.created_at.desc()).limit(1)
    )
    export = export_result.scalar_one()
    assert export.status == "completed"
    assert export.verified is True
    assert export.verification_error is None
    assert export.utterance_count == 2
    assert export.conversation_count == 1
    assert export.speaker_count == 2
    assert export.sha256
