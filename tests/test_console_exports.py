import base64
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMEZONE
from app.db import get_engine
from app.main import app
from app.models.admin import AdminExport, ModerationSettings
from app.models.response import (
    Conversation,
    DailyPrompt,
    InstructionTemplate,
    PromptIssue,
    Speaker,
    SummarizationPrompt,
    SystemPrompt,
    Utterance,
    WeeklySummary,
)
from app.response.utils import prompt_sha256


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


# ── v2 ────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 3, 9, 12, 0, 0, tzinfo=DEFAULT_TIMEZONE)  # a Monday


def _snapshot(prompt: str, day: int, history: int) -> dict[str, object]:
    return {
        "version": 2,
        "provider": "bedrock",
        "model_id": "llama-a",
        "system_prompt": prompt,
        "chat_history": [{"role": "user", "content": f"m{i}"} for i in range(history)],
        "query": "q",
        "week_start": "2026-03-08",
        "day_number": day,
        "user_local_time": None,
    }


async def _seed_v2(session: AsyncSession) -> None:
    """2 users, 2 days, 2 system-prompt versions, a moderated reply, a missing
    daily prompt, and a failed-then-retried reply. u1 replies are pre-change
    (snapshot only); u2 replies carry provenance."""
    day = timedelta(days=1)
    session.add_all(
        [
            Speaker(id="u1", meta={"type": "user"}),
            Speaker(id="bot:u1", meta={"type": "bot"}),
            Speaker(id="u2", meta={"type": "user"}),
            Speaker(id="bot:u2", meta={"type": "bot"}),
            SystemPrompt(
                id="sp1",
                prompt="P1",
                provider="bedrock",
                model_id="llama-a",
                created_at=_T0 - 10 * day,
            ),
            SystemPrompt(
                id="sp2",
                prompt="P2",
                provider="bedrock",
                model_id="llama-b",
                created_at=_T0 + day / 2,
            ),
            InstructionTemplate(id="tpl", template="{base}", created_at=_T0 - 10 * day),
            SummarizationPrompt(id="sum", prompt="S", created_at=_T0 - 10 * day),
            DailyPrompt(id="dp1", day_number=1, content="d1", created_at=_T0 - 10 * day),
            ModerationSettings(id=1, email_enabled=True, thresholds={"violence": 0.5}),
        ]
    )
    await session.commit()
    session.add_all(
        [
            Conversation(id="c1", owner_speaker_id="u1", last_activity_at=_T0 + day),
            Conversation(id="c2", owner_speaker_id="u2", last_activity_at=_T0 + day),
            WeeklySummary(
                id="ws1",
                user_id="u1",
                week_start=date(2026, 3, 1),
                summary="u1 week",
                created_at=_T0 - 2 * day,
            ),
            WeeklySummary(
                id="ws0",
                user_id="u1",
                week_start=date(2026, 2, 22),
                summary="u1 older",
                created_at=_T0 - 9 * day,
            ),
        ]
    )
    await session.commit()
    t = _T0
    session.add_all(
        [
            # u1 day 1: pre-change reply, prompt v1.
            Utterance(
                id="a1",
                conversation_id="c1",
                speaker_id="u1",
                timestamp=t,
                status="received",
                text="hi",
                meta={"day_number": 1},
            ),
            Utterance(
                id="a2",
                conversation_id="c1",
                speaker_id="bot:u1",
                reply_to_id="a1",
                timestamp=t + timedelta(seconds=5),
                status="sent",
                text="hello",
                attempts=1,
                meta={"texet_generation": _snapshot("P1 composed", 1, 30)},
            ),
            # u1 day 2: pre-change reply; no daily prompt for day 2.
            Utterance(
                id="a3",
                conversation_id="c1",
                speaker_id="u1",
                timestamp=t + day,
                status="received",
                text="again",
                meta={"day_number": 2},
            ),
            Utterance(
                id="a4",
                conversation_id="c1",
                speaker_id="bot:u1",
                reply_to_id="a3",
                timestamp=t + day + timedelta(seconds=5),
                status="sent",
                text="yo",
                attempts=1,
                meta={"texet_generation": _snapshot("P2 composed", 2, 40)},
            ),
            # u2 day 1: post-change, failed once then retried.
            Utterance(
                id="b1",
                conversation_id="c2",
                speaker_id="u2",
                timestamp=t,
                status="received",
                text="hey",
                meta={"day_number": 1},
            ),
            Utterance(
                id="b2",
                conversation_id="c2",
                speaker_id="bot:u2",
                reply_to_id="b1",
                timestamp=t + timedelta(seconds=5),
                status="sent",
                text="hi u2",
                attempts=2,
                claimed_at=t + timedelta(minutes=3),
                sent_at=t + timedelta(minutes=3, seconds=2),
                meta={
                    "texet_generation": _snapshot("P1 composed", 1, 2),
                    "texet_provider": "bedrock",
                    "texet_model_id": "llama-a",
                    "texet_system_prompt_id": "sp1",
                    "texet_instruction_template_id": "tpl",
                    "texet_daily_prompt_id": "dp1",
                    "texet_weekly_summary_id": None,
                    "texet_week_start": "2026-03-08",
                    "texet_day_number": 1,
                    "texet_system_prompt_sha256": "stored-hash",
                },
            ),
            # u2 day 2: post-change, bot reply moderated.
            Utterance(
                id="b3",
                conversation_id="c2",
                speaker_id="u2",
                timestamp=t + day,
                status="received",
                text="bad",
                meta={"day_number": 2},
            ),
            Utterance(
                id="b4",
                conversation_id="c2",
                speaker_id="bot:u2",
                reply_to_id="b3",
                timestamp=t + day + timedelta(seconds=5),
                status="moderated",
                text="raw",
                attempts=1,
                sent_at=t + day + timedelta(seconds=6),
                meta={
                    "texet_moderation_source": "bot",
                    "texet_model_id": "llama-b",
                    "texet_daily_prompt_id": None,
                    "texet_daily_prompt_missing": True,
                    "texet_system_prompt_sha256": "h4",
                },
            ),
        ]
    )
    await session.commit()
    session.add_all(
        [
            PromptIssue(
                kind="daily_prompt_missing",
                user_id="u1",
                utterance_id="a3",
                detail="No daily prompt is configured for day 2.",
            ),
            PromptIssue(
                kind="daily_prompt_missing",
                user_id="u2",
                utterance_id="b3",
                detail="No daily prompt is configured for day 2.",
            ),
        ]
    )
    await session.commit()


async def _export(client: AsyncClient, **form: str) -> tuple[zipfile.ZipFile, str]:
    response = await client.post(
        "/console/exports", headers=_basic_auth_header("admin", "secret"), data=form
    )
    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return archive, archive.namelist()[0].split("/")[0]


def _utterances(archive: zipfile.ZipFile, root: str) -> dict[str, dict[str, object]]:
    lines = archive.read(f"{root}/utterances.jsonl").decode().splitlines()
    return {row["id"]: row for row in map(json.loads, lines)}


@pytest.mark.asyncio
async def test_export_v2_default(console_client: AsyncClient, async_session: AsyncSession) -> None:
    await _seed_v2(async_session)
    archive, root = await _export(console_client)
    rows = _utterances(archive, root)
    metas = {k: v["meta"] for k, v in rows.items()}

    # Pre-change replies: inferred where data allows, flagged.
    a2 = metas["a2"]
    assert a2["texet_config_inferred"] is True
    assert a2["texet_daily_prompt_id"] == "dp1"
    assert a2["texet_weekly_summary_id"] == "ws1"  # previous week, created before reply
    assert a2["texet_system_prompt_id"] is None  # tables are edited in place
    assert a2["texet_model_id"] == "llama-a"
    assert a2["texet_system_prompt_sha256"] == prompt_sha256("P1 composed")
    a4 = metas["a4"]
    assert a4["texet_daily_prompt_id"] is None
    assert a4["texet_daily_prompt_missing"] is True
    assert a4["texet_system_prompt_sha256"] == prompt_sha256("P2 composed")
    assert a2["texet_system_prompt_sha256"] != a4["texet_system_prompt_sha256"]

    # Post-change replies: stored values pass through, not inferred.
    b2 = metas["b2"]
    assert "texet_config_inferred" not in b2
    assert b2["texet_system_prompt_id"] == "sp1"
    assert b2["texet_system_prompt_sha256"] == "stored-hash"
    assert b2["texet_attempts"] == 2
    assert datetime.fromisoformat(b2["texet_claimed_at"]) == _T0 + timedelta(minutes=3)
    assert datetime.fromisoformat(b2["texet_sent_at"]) == _T0 + timedelta(minutes=3, seconds=2)
    b4 = metas["b4"]
    assert b4["texet_status"] == "moderated"
    assert b4["texet_daily_prompt_missing"] is True
    assert "texet_config_inferred" not in b4

    # Old rows: no sent_at.
    assert a2["texet_sent_at"] is None

    # Slim snapshot by default.
    for key in ("a2", "a4", "b2"):
        generation = metas[key]["texet_generation"]
        assert "system_prompt" not in generation and "chat_history" not in generation
        assert generation["query"] == "q"

    # Conversation meta: owner's weekly summaries, sorted by week.
    conversations = json.loads(archive.read(f"{root}/conversations.json"))
    c1 = conversations["a1"]
    assert [s["id"] for s in c1["texet_weekly_summaries"]] == ["ws0", "ws1"]
    assert set(c1["texet_weekly_summaries"][0]) == {"id", "week_start", "summary", "created_at"}
    assert conversations["b1"]["texet_weekly_summaries"] == []

    # Speaker meta: first/last message for user speakers only.
    speakers = json.loads(archive.read(f"{root}/speakers.json"))
    assert datetime.fromisoformat(speakers["u1"]["texet_first_message_at"]) == _T0
    assert datetime.fromisoformat(speakers["u1"]["texet_last_message_at"]) == _T0 + timedelta(
        days=1
    )
    assert "texet_first_message_at" not in speakers["bot:u1"]

    # Config file.
    config = json.loads(archive.read(f"{root}/texet_config.json"))
    assert config["export_version"] == 2
    assert [p["id"] for p in config["system_prompts"]] == ["sp1", "sp2"]
    assert set(config["system_prompts"][0]) >= {
        "id",
        "prompt",
        "provider",
        "model_id",
        "created_at",
    }
    assert [t["id"] for t in config["instruction_templates"]] == ["tpl"]
    assert [p["id"] for p in config["summarization_prompts"]] == ["sum"]
    assert config["daily_prompts"][0]["day_number"] == 1
    assert config["moderation_settings"]["thresholds"] == {"violence": 0.5}
    assert "history" in config["moderation_settings_note"]
    assert {i["utterance_id"] for i in config["prompt_issues"]} == {"a3", "b3"}

    corpus = json.loads(archive.read(f"{root}/corpus.json"))
    assert corpus["texet_export_version"] == 2
    assert corpus["texet_include_generation_snapshot"] is False
    assert "texet_git_commit" in corpus


@pytest.mark.asyncio
async def test_export_v2_full_snapshot(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    await _seed_v2(async_session)
    archive, root = await _export(console_client, include_generation_snapshot="on")
    generation = _utterances(archive, root)["a2"]["meta"]["texet_generation"]
    assert generation["system_prompt"] == "P1 composed"
    assert len(generation["chat_history"]) == 30
    corpus = json.loads(archive.read(f"{root}/corpus.json"))
    assert corpus["texet_include_generation_snapshot"] is True


def test_sent_at_migration_up_and_down() -> None:
    import asyncio

    from alembic.config import Config
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from alembic import command

    url = os.environ["DATABASE_URL_TEST"]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    async def _columns() -> set[str]:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: inspect(c).get_columns("utterances"))
        await engine.dispose()
        return {c["name"] for c in cols}

    os.environ["DATABASE_URL"] = url
    command.downgrade(config, "89d36122d523")  # the revision before 50f6b01aaf1e
    assert "sent_at" not in asyncio.run(_columns())
    command.upgrade(config, "head")
    assert "sent_at" in asyncio.run(_columns())
