from __future__ import annotations

import base64
import csv
import datetime
import io
import json
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqladmin.filters import AllUniqueStringValuesFilter, StaticValuesFilter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import (
    DEFAULT_TIMEZONE,
    UTTERANCE_STATUSES,
    admin_enabled,
    get_admin_export_max_rows,
    get_admin_password,
    get_admin_secret_key,
    get_admin_session_ttl_seconds,
    get_admin_username,
)
from app.db import get_async_session, get_engine
from app.models import Conversation, Speaker, Utterance

AdminFormat = Literal["csv", "json"]

router = APIRouter(prefix="/admin/export", tags=["admin"])


def _now() -> datetime.datetime:
    return datetime.datetime.now(DEFAULT_TIMEZONE)


def _parse_datetime(name: str, value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {name} datetime; expected ISO 8601.",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TIMEZONE)
    return parsed


def _credentials_valid(username: str, password: str) -> bool:
    expected_user = get_admin_username()
    expected_pass = get_admin_password()
    if not expected_user or not expected_pass:
        return False
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(
        password, expected_pass
    )


def _basic_auth_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("Authorization", "")
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _session_valid(request: Request) -> bool:
    session = request.scope.get("session")
    if not isinstance(session, dict):
        return False
    user = session.get("admin_user")
    login_at = session.get("admin_login_at")
    if not user or not login_at:
        return False
    if user != get_admin_username():
        return False
    try:
        login_time = datetime.datetime.fromisoformat(login_at)
    except ValueError:
        return False
    if login_time.tzinfo is None:
        login_time = login_time.replace(tzinfo=DEFAULT_TIMEZONE)
    max_age = datetime.timedelta(seconds=get_admin_session_ttl_seconds())
    if _now() - login_time > max_age:
        session.clear()
        return False
    return True


def _authorized(request: Request) -> bool:
    if _session_valid(request):
        return True
    creds = _basic_auth_credentials(request)
    if not creds:
        return False
    return _credentials_valid(*creds)


async def require_admin(request: Request) -> None:
    if _authorized(request):
        return
    raise HTTPException(
        status_code=401,
        detail="Unauthorized.",
        headers={"WWW-Authenticate": "Basic"},
    )


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        if not _credentials_valid(username, password):
            return False
        request.session.update(
            {
                "admin_user": username,
                "admin_login_at": _now().isoformat(),
            }
        )
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return _authorized(request)


class SpeakerAdmin(ModelView, model=Speaker):
    name = "Speaker"
    name_plural = "Speakers"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    page_size = 50
    column_list = ["id", "created_at"]
    column_details_list = ["id", "created_at", "meta"]
    column_searchable_list = ["id"]
    column_labels = {"id": "Speaker ID", "created_at": "Created At"}


class ConversationAdmin(ModelView, model=Conversation):
    name = "Conversation"
    name_plural = "Conversations"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    page_size = 50
    column_list = ["id", "owner_speaker_id", "status", "last_activity_at"]
    column_details_list = [
        "id",
        "owner_speaker_id",
        "status",
        "last_activity_at",
        "created_at",
        "meta",
    ]
    column_searchable_list = ["id", "owner_speaker_id"]
    column_filters = [AllUniqueStringValuesFilter(Conversation.status, title="Status")]
    column_labels = {
        "id": "Conversation ID",
        "owner_speaker_id": "Owner Speaker ID",
        "last_activity_at": "Last Activity",
        "created_at": "Created At",
    }


class UtteranceAdmin(ModelView, model=Utterance):
    name = "Utterance"
    name_plural = "Utterances"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    page_size = 50
    column_list = [
        "id",
        "conversation_id",
        "speaker_id",
        "timestamp",
        "status",
        "error",
    ]
    column_details_list = [
        "id",
        "conversation_id",
        "speaker_id",
        "reply_to_id",
        "timestamp",
        "status",
        "text",
        "error",
        "created_at",
        "meta",
    ]
    column_searchable_list = ["text", "speaker_id", "conversation_id"]
    column_filters = [
        StaticValuesFilter(
            Utterance.status,
            values=[(status, status) for status in UTTERANCE_STATUSES],
            title="Status",
        )
    ]
    column_labels = {
        "id": "Utterance ID",
        "conversation_id": "Conversation ID",
        "speaker_id": "Speaker ID",
        "reply_to_id": "Reply To",
        "timestamp": "Timestamp",
        "created_at": "Created At",
    }


def _serialize_meta(meta: dict[str, Any] | None) -> str:
    if meta is None:
        return ""
    return json.dumps(meta, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _serialize_datetime(value: datetime.datetime | None) -> str:
    if not value:
        return ""
    return value.isoformat()


def _csv_response(
    filename: str, headers: list[str], rows: list[list[str]]
) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    yield_value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    def row_stream() -> Any:
        yield yield_value
        for row in rows:
            writer.writerow(row)
            data = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            yield data

    response = StreamingResponse(row_stream(), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _json_response(filename: str, payload: list[dict[str, Any]]) -> JSONResponse:
    response = JSONResponse(content=payload)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _limit_from_query(limit: int | None) -> int:
    max_rows = get_admin_export_max_rows()
    if not limit or limit <= 0:
        return max_rows
    return min(limit, max_rows)


@router.get("/utterances", response_model=None)
async def export_utterances(
    format: AdminFormat = "csv",
    status: str | None = None,
    speaker_id: str | None = None,
    conversation_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_meta: bool = False,
    limit: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> Response:
    if status and status not in UTTERANCE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status filter.")

    since_dt = _parse_datetime("since", since)
    until_dt = _parse_datetime("until", until)

    query = select(Utterance).order_by(Utterance.timestamp)
    if status:
        query = query.where(Utterance.status == status)
    if speaker_id:
        query = query.where(Utterance.speaker_id == speaker_id)
    if conversation_id:
        query = query.where(Utterance.conversation_id == conversation_id)
    if since_dt:
        query = query.where(Utterance.timestamp >= since_dt)
    if until_dt:
        query = query.where(Utterance.timestamp <= until_dt)

    query = query.limit(_limit_from_query(limit))
    result = await session.execute(query)
    utterances = result.scalars().all()

    if format == "json":
        payload = [
            {
                "id": utterance.id,
                "conversation_id": utterance.conversation_id,
                "speaker_id": utterance.speaker_id,
                "reply_to_id": utterance.reply_to_id or "",
                "timestamp": _serialize_datetime(utterance.timestamp),
                "status": utterance.status,
                "text": utterance.text or "",
                "error": utterance.error or "",
                "created_at": _serialize_datetime(utterance.created_at),
                "meta": utterance.meta if include_meta else None,
            }
            for utterance in utterances
        ]
        return _json_response("utterances.json", payload)

    headers = [
        "id",
        "conversation_id",
        "speaker_id",
        "reply_to_id",
        "timestamp",
        "status",
        "text",
        "error",
        "created_at",
    ]
    if include_meta:
        headers.append("meta")

    rows: list[list[str]] = []
    for utterance in utterances:
        row = [
            utterance.id,
            utterance.conversation_id,
            utterance.speaker_id,
            utterance.reply_to_id or "",
            _serialize_datetime(utterance.timestamp),
            utterance.status,
            utterance.text or "",
            utterance.error or "",
            _serialize_datetime(utterance.created_at),
        ]
        if include_meta:
            row.append(_serialize_meta(utterance.meta))
        rows.append(row)

    return _csv_response("utterances.csv", headers, rows)


@router.get("/conversations", response_model=None)
async def export_conversations(
    format: AdminFormat = "csv",
    status: str | None = None,
    owner_speaker_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_meta: bool = False,
    limit: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> Response:
    since_dt = _parse_datetime("since", since)
    until_dt = _parse_datetime("until", until)

    query = select(Conversation).order_by(Conversation.last_activity_at)
    if status:
        query = query.where(Conversation.status == status)
    if owner_speaker_id:
        query = query.where(Conversation.owner_speaker_id == owner_speaker_id)
    if since_dt:
        query = query.where(Conversation.last_activity_at >= since_dt)
    if until_dt:
        query = query.where(Conversation.last_activity_at <= until_dt)

    query = query.limit(_limit_from_query(limit))
    result = await session.execute(query)
    conversations = result.scalars().all()

    if format == "json":
        payload = [
            {
                "id": conversation.id,
                "owner_speaker_id": conversation.owner_speaker_id,
                "status": conversation.status,
                "last_activity_at": _serialize_datetime(conversation.last_activity_at),
                "created_at": _serialize_datetime(conversation.created_at),
                "meta": conversation.meta if include_meta else None,
            }
            for conversation in conversations
        ]
        return _json_response("conversations.json", payload)

    headers = ["id", "owner_speaker_id", "status", "last_activity_at", "created_at"]
    if include_meta:
        headers.append("meta")

    rows: list[list[str]] = []
    for conversation in conversations:
        row = [
            conversation.id,
            conversation.owner_speaker_id,
            conversation.status,
            _serialize_datetime(conversation.last_activity_at),
            _serialize_datetime(conversation.created_at),
        ]
        if include_meta:
            row.append(_serialize_meta(conversation.meta))
        rows.append(row)

    return _csv_response("conversations.csv", headers, rows)


@router.get("/speakers", response_model=None)
async def export_speakers(
    format: AdminFormat = "csv",
    speaker_id: str | None = None,
    include_meta: bool = False,
    limit: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> Response:
    query = select(Speaker).order_by(Speaker.created_at)
    if speaker_id:
        query = query.where(Speaker.id == speaker_id)

    query = query.limit(_limit_from_query(limit))
    result = await session.execute(query)
    speakers = result.scalars().all()

    if format == "json":
        payload = [
            {
                "id": speaker.id,
                "created_at": _serialize_datetime(speaker.created_at),
                "meta": speaker.meta if include_meta else None,
            }
            for speaker in speakers
        ]
        return _json_response("speakers.json", payload)

    headers = ["id", "created_at"]
    if include_meta:
        headers.append("meta")

    rows: list[list[str]] = []
    for speaker in speakers:
        row = [speaker.id, _serialize_datetime(speaker.created_at)]
        if include_meta:
            row.append(_serialize_meta(speaker.meta))
        rows.append(row)

    return _csv_response("speakers.csv", headers, rows)


def init_admin(app: FastAPI) -> None:
    if getattr(app.state, "admin_initialized", False):
        return
    if not admin_enabled():
        return

    secret_key = get_admin_secret_key()
    if not secret_key:
        return

    authentication_backend = AdminAuth(secret_key=secret_key)
    admin = Admin(app, get_engine(), authentication_backend=authentication_backend)
    admin.add_view(SpeakerAdmin)
    admin.add_view(ConversationAdmin)
    admin.add_view(UtteranceAdmin)
    app.state.admin = admin
    app.state.admin_initialized = True
