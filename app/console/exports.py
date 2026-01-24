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
from sqlalchemy import select
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
from app.models.admin import AdminExport
from app.models.response import Conversation, Speaker, Utterance

CORPUS_PREFIX = "texet_convokit"


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


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")))
            handle.write("\n")


def _verify_corpus_dir(
    corpus_dir: Path, expected: ExportCounts
) -> tuple[bool, str | None]:
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
    return True, None


async def build_convokit_export(
    session: AsyncSession,
    export_id: str,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
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
        speaker_result = await session.execute(
            select(Speaker).where(Speaker.id.in_(speaker_ids))
        )
        speakers = list(speaker_result.scalars().all())

    conversations: list[Conversation] = []
    if conversation_ids:
        conversation_result = await session.execute(
            select(Conversation).where(Conversation.id.in_(conversation_ids))
        )
        conversations = list(conversation_result.scalars().all())

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
        }
        if utterance.reply_to_id and reply_to is None:
            meta_updates["texet_reply_to_missing"] = utterance.reply_to_id

        meta = _merge_meta(utterance.meta, meta_updates)
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
        meta = _merge_meta(
            speaker.meta,
            {"texet_created_at": _iso(speaker.created_at)},
        )
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
    }

    temp_dir = Path(tempfile.mkdtemp(prefix=f"{CORPUS_PREFIX}_"))
    corpus_dir = temp_dir / f"{CORPUS_PREFIX}_{export_id}"
    try:
        corpus_dir.mkdir(parents=True, exist_ok=True)

        _write_jsonl(corpus_dir / "utterances.jsonl", utterance_rows)
        _write_json(corpus_dir / "speakers.json", speaker_rows)
        _write_json(corpus_dir / "conversations.json", conversation_rows)
        _write_json(corpus_dir / "corpus.json", corpus_meta)

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
        rows = "<tr><td colspan=\"10\">No exports yet.</td></tr>"

    error_block = ""
    if error_message:
        error_block = f"<p class=\"error\">{_escape(error_message)}</p>"

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


@console_router.post("/exports")
async def console_exports_create(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> FileResponse:
    form = await request.form()
    since = _parse_datetime("since", str(form.get("since") or "").strip() or None)
    until = _parse_datetime("until", str(form.get("until") or "").strip() or None)
    if since and until and since > until:
        raise HTTPException(status_code=400, detail="Start must be before end.")

    export = AdminExport(
        kind="convokit",
        status="started",
        range_start=since,
        range_end=until,
    )
    session.add(export)
    await session.flush()
    export_id = export.id
    await session.commit()

    try:
        artifact = await build_convokit_export(
            session, export_id=export_id, since=since, until=until
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
    export_record.meta = artifact.meta
    await session.commit()

    if not artifact.verified:
        shutil.rmtree(artifact.temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Export validation failed.")

    background_tasks.add_task(shutil.rmtree, artifact.temp_dir, ignore_errors=True)
    return FileResponse(
        artifact.zip_path,
        media_type="application/zip",
        filename=artifact.zip_path.name,
    )
