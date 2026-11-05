from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import CONSOLE_PREFIX, DEFAULT_TIMEZONE
from app.console.core import (
    _escape,
    _now,
    _parse_datetime,
    _serialize_datetime,
    console_router,
    require_admin,
)
from app.db import get_async_session
from app.models.admin import AdminExport, ModerationSettings
from app.models.base import Base
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
from app.response.crud import BOT_SPEAKER_PREFIX
from app.response.service import PROMPT_ISSUE_DAILY_PROMPT_MISSING
from app.response.utils import prompt_sha256

CORPUS_PREFIX = "texet_convokit"
EXPORT_VERSION = 2
CONFIG_FILE = "texet_config.json"
# Bulky snapshot fields: chat_history repeats the whole week on every reply,
# so keeping it makes the export grow quadratically.
SLIM_GENERATION_KEYS = ("system_prompt", "chat_history")
_GIT_COMMIT_FILE = Path(__file__).resolve().parents[1] / "GIT_COMMIT"

# Where each model column goes. "table.*" means every column of the table is
# written by _row_dict, so new columns there are exported automatically.
# tests/test_export_columns.py fails on any column missing from both maps.
EXPORTED: dict[str, str] = {
    "speakers.id": "speakers.json key; utterance speaker",
    "speakers.meta": "speakers.json meta",
    "speakers.created_at": "speakers.json texet_created_at",
    "conversations.id": "conversations.json texet_conversation_id",
    "conversations.owner_speaker_id": "conversations.json texet_owner_speaker_id",
    "conversations.status": "conversations.json texet_status",
    "conversations.last_activity_at": "conversations.json texet_last_activity_at",
    "conversations.meta": "conversations.json meta",
    "conversations.created_at": "conversations.json texet_created_at",
    "utterances.id": "utterances.jsonl id",
    "utterances.conversation_id": "utterances.jsonl conversation_id (first utterance id)",
    "utterances.speaker_id": "utterances.jsonl speaker",
    "utterances.reply_to_id": "utterances.jsonl reply_to, or texet_reply_to_missing",
    "utterances.timestamp": "utterances.jsonl timestamp (epoch seconds)",
    "utterances.status": "utterance meta texet_status",
    "utterances.claimed_at": "utterance meta texet_claimed_at",
    "utterances.attempts": "utterance meta texet_attempts",
    "utterances.sent_at": "utterance meta texet_sent_at",
    "utterances.text": "utterances.jsonl text",
    "utterances.error": "utterance meta texet_error",
    "utterances.meta": "utterances.jsonl meta (generation snapshot slimmed by default)",
    "utterances.created_at": "utterance meta texet_created_at",
    "weekly_summaries.id": "conversations.json texet_weekly_summaries[].id",
    "weekly_summaries.user_id": "implied: the conversation owner",
    "weekly_summaries.week_start": "conversations.json texet_weekly_summaries[].week_start",
    "weekly_summaries.summary": "conversations.json texet_weekly_summaries[].summary",
    "weekly_summaries.created_at": "conversations.json texet_weekly_summaries[].created_at",
    "system_prompts.*": f"{CONFIG_FILE} system_prompts",
    "instruction_templates.*": f"{CONFIG_FILE} instruction_templates",
    "summarization_prompts.*": f"{CONFIG_FILE} summarization_prompts",
    "daily_prompts.*": f"{CONFIG_FILE} daily_prompts",
    "moderation_settings.*": f"{CONFIG_FILE} moderation_settings",
    "prompt_issues.*": f"{CONFIG_FILE} prompt_issues",
}

EXPORT_EXCLUDED: dict[str, str] = {
    "api_keys.id": "credential metadata, not study data",
    "api_keys.name": "credential metadata, not study data",
    "api_keys.key_hash": "secret",
    "api_keys.key_prefix": "partial secret",
    "api_keys.is_active": "credential metadata, not study data",
    "api_keys.last_used_at": "credential metadata, not study data",
    "api_keys.scope": "credential metadata, not study data",
    "api_keys.created_at": "credential metadata, not study data",
    "admin_exports.*": "the export audit log itself, not study data",
}


@dataclass(frozen=True)
class ExportCounts:
    utterances: int
    conversations: int
    speakers: int
    reply_to_missing: int


@dataclass(frozen=True)
class ExportArtifact:
    zip_path: Path
    temp_dir: Path
    counts: ExportCounts
    verified: bool
    verification_error: str | None
    sha256: str
    meta: dict[str, Any]


def _ensure_tz(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=DEFAULT_TIMEZONE)
    return value


def _iso(value: datetime.datetime | None) -> str | None:
    if not value:
        return None
    return _ensure_tz(value).isoformat()


def _epoch(value: datetime.datetime) -> int:
    return int(_ensure_tz(value).timestamp())


def _type_string(value: Any) -> str:
    return f"<class '{type(value).__name__}'>"


def _merge_meta(base: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if base:
        merged.update(base)
    merged.update(updates)
    return merged


def _meta_index(metas: Iterable[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for meta in metas:
        for key, value in meta.items():
            if value is None:
                continue
            value_type = _type_string(value)
            existing = index.get(key)
            if existing is None:
                index[key] = value_type
            elif existing != value_type:
                index[key] = "<class 'object'>"
    return index


def slim_generation(meta: dict[str, Any], *, include_generation_snapshot: bool) -> dict[str, Any]:
    """Add the composed-prompt hash; drop the bulky snapshot fields unless asked."""
    generation = meta.get("texet_generation")
    if not isinstance(generation, dict):
        return meta
    out = dict(meta)
    prompt = generation.get("system_prompt")
    if isinstance(prompt, str):
        out.setdefault("texet_system_prompt_sha256", prompt_sha256(prompt))
    if not include_generation_snapshot:
        out["texet_generation"] = {
            k: v for k, v in generation.items() if k not in SLIM_GENERATION_KEYS
        }
    return out


def infer_provenance(
    meta: dict[str, Any],
    *,
    timestamp: datetime.datetime,
    owner_id: str,
    reply_to_id: str | None,
    daily_prompts: dict[int, DailyPrompt],
    summaries: dict[tuple[str, datetime.date], tuple[str, datetime.datetime]],
    daily_missing_for: set[str],
) -> dict[str, Any]:
    """Provenance for bot replies stored before replies recorded it.

    System-prompt and template ids stay None: the console edits those rows in
    place, so "latest row by created_at" can name text that never ran. Daily
    prompt and weekly summary are matched only when the row already existed.
    """
    generation = meta.get("texet_generation")
    if not isinstance(generation, dict) or "texet_model_id" in meta:
        return {}

    def existed(created_at: datetime.datetime) -> bool:
        return _ensure_tz(created_at) <= _ensure_tz(timestamp)

    day_number = generation.get("day_number")
    daily = daily_prompts.get(day_number) if isinstance(day_number, int) else None
    week_start = generation.get("week_start")
    summary = None
    if isinstance(week_start, str):
        # The prompt injects the previous week's summary.
        previous = datetime.date.fromisoformat(week_start) - datetime.timedelta(days=7)
        summary = summaries.get((owner_id, previous))

    inferred: dict[str, Any] = {
        "texet_provider": generation.get("provider"),
        "texet_model_id": generation.get("model_id"),
        "texet_system_prompt_id": None,
        "texet_instruction_template_id": None,
        "texet_daily_prompt_id": daily.id if daily and existed(daily.created_at) else None,
        "texet_weekly_summary_id": summary[0] if summary and existed(summary[1]) else None,
        "texet_week_start": week_start,
        "texet_day_number": day_number,
        "texet_config_inferred": True,
    }
    if reply_to_id in daily_missing_for:
        inferred["texet_daily_prompt_missing"] = True
    return inferred


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return _iso(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def _row_dict(row: Base) -> dict[str, Any]:
    return {
        attr.key: _jsonable(getattr(row, attr.key)) for attr in inspect(row).mapper.column_attrs
    }


def _git_commit() -> str | None:
    # git archive (the EB deploy package) fills this in via export-subst.
    try:
        value = _GIT_COMMIT_FILE.read_text().strip()
    except OSError:
        return None
    return None if not value or value.startswith("$Format") else value


async def _all(session: AsyncSession, model: type[Base], *order: Any) -> list[Any]:
    return list((await session.execute(select(model).order_by(*order))).scalars().all())


async def _build_config(
    session: AsyncSession,
) -> tuple[dict[str, Any], list[DailyPrompt], set[str]]:
    daily_prompts = await _all(session, DailyPrompt, DailyPrompt.day_number)
    issues = await _all(session, PromptIssue, PromptIssue.created_at, PromptIssue.id)
    moderation = await session.get(ModerationSettings, 1)
    config = {
        "export_version": EXPORT_VERSION,
        "note": (
            "The console edits these rows in place and can delete them, so the text here is "
            "the current value, not always the text that produced an older reply. Utterance "
            "meta texet_system_prompt_sha256 hashes the composed prompt, which includes the "
            "participant's summary, the daily prompt and the local time. It identifies the "
            "exact prompt sent to the model and does not identify a config version."
        ),
        "system_prompts": [
            _row_dict(r)
            for r in await _all(session, SystemPrompt, SystemPrompt.created_at, SystemPrompt.id)
        ],
        "instruction_templates": [
            _row_dict(r)
            for r in await _all(
                session, InstructionTemplate, InstructionTemplate.created_at, InstructionTemplate.id
            )
        ],
        "summarization_prompts": [
            _row_dict(r)
            for r in await _all(
                session, SummarizationPrompt, SummarizationPrompt.created_at, SummarizationPrompt.id
            )
        ],
        "daily_prompts": [_row_dict(r) for r in daily_prompts],
        "moderation_settings": _row_dict(moderation) if moderation else None,
        "moderation_settings_note": "Current row only. TexET does not store the history.",
        "prompt_issues": [_row_dict(r) for r in issues],
    }
    missing = {
        i.utterance_id
        for i in issues
        if i.kind == PROMPT_ISSUE_DAILY_PROMPT_MISSING and i.utterance_id
    }
    return config, daily_prompts, missing


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")))
            handle.write("\n")


def _verify_corpus_dir(corpus_dir: Path, expected: ExportCounts) -> tuple[bool, str | None]:
    utterances_path = corpus_dir / "utterances.jsonl"
    speakers_path = corpus_dir / "speakers.json"
    conversations_path = corpus_dir / "conversations.json"

    utterance_count = 0
    if utterances_path.exists():
        with utterances_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    utterance_count += 1

    speaker_count = 0
    if speakers_path.exists():
        speakers = json.loads(speakers_path.read_text())
        if isinstance(speakers, dict):
            speaker_count = len(speakers)

    conversation_count = 0
    if conversations_path.exists():
        conversations = json.loads(conversations_path.read_text())
        if isinstance(conversations, dict):
            conversation_count = len(conversations)

    if utterance_count != expected.utterances:
        return False, "Utterance count mismatch."
    if speaker_count != expected.speakers:
        return False, "Speaker count mismatch."
    if conversation_count != expected.conversations:
        return False, "Conversation count mismatch."
    try:
        json.loads((corpus_dir / CONFIG_FILE).read_text())
    except (OSError, ValueError):
        return False, f"{CONFIG_FILE} is missing or does not parse."
    return True, None


async def build_convokit_export(
    session: AsyncSession,
    export_id: str,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
    include_generation_snapshot: bool = False,
) -> ExportArtifact:
    query = select(Utterance).order_by(Utterance.timestamp, Utterance.id)
    if since:
        query = query.where(Utterance.timestamp >= since)
    if until:
        query = query.where(Utterance.timestamp <= until)

    result = await session.execute(query)
    utterances = result.scalars().all()

    utterance_ids = {utterance.id for utterance in utterances}
    conversation_ids = {utterance.conversation_id for utterance in utterances}
    speaker_ids = {utterance.speaker_id for utterance in utterances}
    conversation_first_utterance: dict[str, str] = {}
    for utterance in utterances:
        conversation_first_utterance.setdefault(utterance.conversation_id, utterance.id)

    speakers: list[Speaker] = []
    if speaker_ids:
        speaker_result = await session.execute(select(Speaker).where(Speaker.id.in_(speaker_ids)))
        speakers = list(speaker_result.scalars().all())

    conversations: list[Conversation] = []
    if conversation_ids:
        conversation_result = await session.execute(
            select(Conversation).where(Conversation.id.in_(conversation_ids))
        )
        conversations = list(conversation_result.scalars().all())
    owner_of = {c.id: c.owner_speaker_id for c in conversations}

    config, daily_prompt_rows, daily_missing_for = await _build_config(session)
    daily_by_day = {p.day_number: p for p in daily_prompt_rows}

    summary_rows: list[WeeklySummary] = []
    if owner_of:
        summary_result = await session.execute(
            select(WeeklySummary)
            .where(WeeklySummary.user_id.in_(set(owner_of.values())))
            .order_by(WeeklySummary.week_start, WeeklySummary.id)
        )
        summary_rows = list(summary_result.scalars().all())
    summaries_by_week = {(s.user_id, s.week_start): (s.id, s.created_at) for s in summary_rows}

    # All-time, not range-limited: a participant's first and last message.
    user_speaker_ids = {s for s in speaker_ids if not s.startswith(BOT_SPEAKER_PREFIX)}
    message_span: dict[str, tuple[datetime.datetime, datetime.datetime]] = {}
    if user_speaker_ids:
        span_result = await session.execute(
            select(
                Utterance.speaker_id, func.min(Utterance.timestamp), func.max(Utterance.timestamp)
            )
            .where(Utterance.speaker_id.in_(user_speaker_ids))
            .group_by(Utterance.speaker_id)
        )
        message_span = {row[0]: (row[1], row[2]) for row in span_result.all()}

    reply_to_missing = 0
    utterance_rows: list[dict[str, Any]] = []
    utterance_metas: list[dict[str, Any]] = []
    for utterance in utterances:
        reply_to = utterance.reply_to_id
        if reply_to and reply_to not in utterance_ids:
            reply_to_missing += 1
            reply_to = None

        meta_updates = {
            "texet_status": utterance.status,
            "texet_error": utterance.error,
            "texet_created_at": _iso(utterance.created_at),
            "texet_sent_at": _iso(utterance.sent_at),
            "texet_claimed_at": _iso(utterance.claimed_at),
            "texet_attempts": utterance.attempts,
        }
        if utterance.reply_to_id and reply_to is None:
            meta_updates["texet_reply_to_missing"] = utterance.reply_to_id

        meta = _merge_meta(utterance.meta, meta_updates)
        meta.update(
            infer_provenance(
                meta,
                timestamp=utterance.timestamp,
                owner_id=owner_of.get(utterance.conversation_id, ""),
                reply_to_id=utterance.reply_to_id,
                daily_prompts=daily_by_day,
                summaries=summaries_by_week,
                daily_missing_for=daily_missing_for,
            )
        )
        meta = slim_generation(meta, include_generation_snapshot=include_generation_snapshot)
        utterance_metas.append(meta)
        convokit_conversation_id = conversation_first_utterance[utterance.conversation_id]
        utterance_rows.append(
            {
                "id": utterance.id,
                "speaker": utterance.speaker_id,
                "conversation_id": convokit_conversation_id,
                "reply_to": reply_to,
                "timestamp": _epoch(utterance.timestamp),
                "text": utterance.text or "",
                "meta": meta,
            }
        )

    speaker_rows: dict[str, dict[str, Any]] = {}
    speaker_metas: list[dict[str, Any]] = []
    for speaker in speakers:
        speaker_updates: dict[str, Any] = {"texet_created_at": _iso(speaker.created_at)}
        if speaker.id in message_span:
            first, last = message_span[speaker.id]
            speaker_updates["texet_first_message_at"] = _iso(first)
            speaker_updates["texet_last_message_at"] = _iso(last)
        meta = _merge_meta(speaker.meta, speaker_updates)
        speaker_rows[speaker.id] = meta
        speaker_metas.append(meta)

    conversation_rows: dict[str, dict[str, Any]] = {}
    conversation_metas: list[dict[str, Any]] = []
    for conversation in conversations:
        convokit_id = conversation_first_utterance.get(conversation.id)
        if not convokit_id:
            continue
        meta = _merge_meta(
            conversation.meta,
            {
                "texet_conversation_id": conversation.id,
                "texet_owner_speaker_id": conversation.owner_speaker_id,
                "texet_status": conversation.status,
                "texet_last_activity_at": _iso(conversation.last_activity_at),
                "texet_created_at": _iso(conversation.created_at),
                "texet_weekly_summaries": [
                    {
                        "id": s.id,
                        "week_start": s.week_start.isoformat(),
                        "summary": s.summary,
                        "created_at": _iso(s.created_at),
                    }
                    for s in summary_rows
                    if s.user_id == conversation.owner_speaker_id
                ],
            },
        )
        conversation_rows[convokit_id] = meta
        conversation_metas.append(meta)

    counts = ExportCounts(
        utterances=len(utterances),
        conversations=len(conversation_rows),
        speakers=len(speakers),
        reply_to_missing=reply_to_missing,
    )

    corpus_meta = {
        "texet_exported_at": _iso(_now()),
        "texet_range_start": _iso(since),
        "texet_range_end": _iso(until),
        "texet_utterance_count": counts.utterances,
        "texet_conversation_count": counts.conversations,
        "texet_speaker_count": counts.speakers,
        "texet_reply_to_missing": counts.reply_to_missing,
        "texet_export_version": EXPORT_VERSION,
        "texet_include_generation_snapshot": include_generation_snapshot,
        "texet_git_commit": _git_commit(),
    }

    temp_dir = Path(tempfile.mkdtemp(prefix=f"{CORPUS_PREFIX}_"))
    corpus_dir = temp_dir / f"{CORPUS_PREFIX}_{export_id}"
    try:
        corpus_dir.mkdir(parents=True, exist_ok=True)

        _write_jsonl(corpus_dir / "utterances.jsonl", utterance_rows)
        _write_json(corpus_dir / "speakers.json", speaker_rows)
        _write_json(corpus_dir / "conversations.json", conversation_rows)
        _write_json(corpus_dir / "corpus.json", corpus_meta)
        _write_json(corpus_dir / CONFIG_FILE, config)

        index_payload = {
            "utterances-index": _meta_index(utterance_metas),
            "speakers-index": _meta_index(speaker_metas),
            "conversations-index": _meta_index(conversation_metas),
            "overall-index": _meta_index([corpus_meta]),
            "version": 1,
        }
        _write_json(corpus_dir / "index.json", index_payload)

        verified, verification_error = _verify_corpus_dir(corpus_dir, counts)

        zip_path = temp_dir / f"{corpus_dir.name}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in corpus_dir.iterdir():
                archive.write(path, arcname=f"{corpus_dir.name}/{path.name}")

        sha256 = hashlib.sha256()
        with zip_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                sha256.update(chunk)

        meta = {
            "corpus_dir": corpus_dir.name,
            "reply_to_missing": counts.reply_to_missing,
            "export_version": EXPORT_VERSION,
            "include_generation_snapshot": include_generation_snapshot,
        }

        return ExportArtifact(
            zip_path=zip_path,
            temp_dir=temp_dir,
            counts=counts,
            verified=verified,
            verification_error=verification_error,
            sha256=sha256.hexdigest(),
            meta=meta,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _render_exports_page(
    exports: list[AdminExport], error_message: str | None = None
) -> HTMLResponse:
    if exports:
        rows = "\n".join(
            f"<tr>"
            f"<td>{_escape(export.id)}</td>"
            f"<td>{_escape(export.status)}</td>"
            f"<td>{_escape(_serialize_datetime(export.range_start))}</td>"
            f"<td>{_escape(_serialize_datetime(export.range_end))}</td>"
            f"<td>{export.utterance_count or 0}</td>"
            f"<td>{export.conversation_count or 0}</td>"
            f"<td>{export.speaker_count or 0}</td>"
            f"<td>{'yes' if export.verified else 'no'}</td>"
            f"<td>{_escape(_serialize_datetime(export.created_at))}</td>"
            f"<td>{_escape(_serialize_datetime(export.completed_at))}</td>"
            f"</tr>"
            for export in exports
        )
    else:
        rows = '<tr><td colspan="10">No exports yet.</td></tr>'

    error_block = ""
    if error_message:
        error_block = f'<p class="error">{_escape(error_message)}</p>'

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Exports</title>
        <style>
          :root{{
            color-scheme:light;
            --bg:#f6f3ef;
            --panel:#fff;
            --text:#1f2328;
            --muted:#5f6b7a;
            --accent:#1c5d99;
            --border:#e5e1da;
            --error:#b42318;
          }}
          *{{box-sizing:border-box}}
          body{{
            margin:0;
            font-family:"SF Pro Text","Segoe UI","Helvetica Neue","Noto Sans",sans-serif;
            color:var(--text);
            background:var(--bg);
          }}
          .wrap{{max-width:980px;margin:0 auto;padding:40px 20px 56px}}
          h1{{margin:0 0 6px;font-size:24px;letter-spacing:-.02em}}
          h2{{margin:24px 0 10px;font-size:16px}}
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .muted{{color:var(--muted);font-size:13px}}
          form{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:12px}}
          label{{font-size:12px;color:var(--muted)}}
          input{{
            padding:8px 10px;
            border:1px solid var(--border);
            border-radius:10px;
            min-width:220px;
          }}
          button{{
            padding:8px 14px;
            border-radius:10px;
            border:1px solid var(--accent);
            background:var(--accent);
            color:#fff;
            font-weight:600;
            cursor:pointer;
          }}
          table{{
            width:100%;
            border-collapse:collapse;
            margin-top:12px;
            border:1px solid var(--border);
            background:var(--panel);
            border-radius:12px;
            overflow:hidden;
          }}
          th,td{{
            padding:10px 12px;
            border-bottom:1px solid var(--border);
            text-align:left;
            font-size:13px;
          }}
          th{{font-size:12px;color:var(--muted);font-weight:600}}
          .error{{margin-top:12px;color:var(--error);font-size:13px}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Exports</h1>
          <p>Export a ConvoKit corpus for a time range (UTC-05:00 if no timezone).</p>
          {error_block}
          <form method="post" action="{CONSOLE_PREFIX}/exports">
            <div>
              <label for="since">Start</label>
              <input type="datetime-local" id="since" name="since" />
            </div>
            <div>
              <label for="until">End</label>
              <input type="datetime-local" id="until" name="until" />
            </div>
            <div>
              <label>
                <input type="checkbox" name="include_generation_snapshot" />
                Full generation snapshot (large)
              </label>
            </div>
            <button type="submit">Export ConvoKit corpus</button>
          </form>
          <h2>Recent exports</h2>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Status</th>
                <th>Start</th>
                <th>End</th>
                <th>Utterances</th>
                <th>Conversations</th>
                <th>Speakers</th>
                <th>Verified</th>
                <th>Created</th>
                <th>Completed</th>
              </tr>
            </thead>
            <tbody>
              {rows}
            </tbody>
          </table>
          <p class="muted" style="margin-top:16px;">
            <a href="{CONSOLE_PREFIX}">Back to console</a>
          </p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html.strip())


@console_router.get("/exports", response_class=HTMLResponse)
async def console_exports(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(AdminExport).order_by(AdminExport.created_at.desc()).limit(25)
    )
    exports = list(result.scalars().all())
    return _render_exports_page(exports)


async def run_logged_export(
    session: AsyncSession,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    include_generation_snapshot: bool,
    record_meta: dict[str, Any] | None = None,
) -> tuple[str, ExportArtifact]:
    """Build an export and log it as an AdminExport row. Raises HTTPException.

    The caller owns artifact.temp_dir and must remove it after sending.
    """
    if since and until and since > until:
        raise HTTPException(status_code=400, detail="Start must be before end.")

    export = AdminExport(kind="convokit", status="started", range_start=since, range_end=until)
    session.add(export)
    await session.flush()
    export_id = export.id
    await session.commit()

    try:
        artifact = await build_convokit_export(
            session,
            export_id=export_id,
            since=since,
            until=until,
            include_generation_snapshot=include_generation_snapshot,
        )
    except Exception as exc:
        await session.rollback()
        export_record = await session.get(AdminExport, export_id)
        if export_record:
            export_record.status = "failed"
            export_record.completed_at = _now()
            export_record.error = str(exc).strip() or exc.__class__.__name__
            await session.commit()
        raise HTTPException(status_code=500, detail="Export failed.") from exc

    export_record = await session.get(AdminExport, export_id)
    if export_record is None:
        shutil.rmtree(artifact.temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Export record missing.")

    export_record.status = "completed" if artifact.verified else "failed"
    export_record.completed_at = _now()
    export_record.utterance_count = artifact.counts.utterances
    export_record.conversation_count = artifact.counts.conversations
    export_record.speaker_count = artifact.counts.speakers
    export_record.verified = artifact.verified
    export_record.verification_error = artifact.verification_error
    export_record.sha256 = artifact.sha256
    export_record.meta = {**artifact.meta, **(record_meta or {})}
    await session.commit()

    if not artifact.verified:
        shutil.rmtree(artifact.temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Export validation failed.")
    return export_id, artifact


@console_router.post("/exports")
async def console_exports_create(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> FileResponse:
    form = await request.form()
    _export_id, artifact = await run_logged_export(
        session,
        since=_parse_datetime("since", str(form.get("since") or "").strip() or None),
        until=_parse_datetime("until", str(form.get("until") or "").strip() or None),
        include_generation_snapshot=bool(form.get("include_generation_snapshot")),
        record_meta={"source": "console"},
    )
    background_tasks.add_task(shutil.rmtree, artifact.temp_dir, ignore_errors=True)
    return FileResponse(
        artifact.zip_path,
        media_type="application/zip",
        filename=artifact.zip_path.name,
        headers={"Cache-Control": "no-store"},
    )
