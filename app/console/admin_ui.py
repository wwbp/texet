from __future__ import annotations

import inspect

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    OperationColumnFilter,
    StaticValuesFilter,
)
from starlette.requests import Request

from app.config import CONSOLE_PREFIX, UTTERANCE_STATUSES, admin_enabled, get_admin_secret_key
from app.console.core import _authorized, _credentials_valid, _now
from app.db import get_engine
from app.models.admin import AdminExport
from app.models.auth import ApiKey
from app.models.response import Conversation, Speaker, Utterance


def _fmt_dt(m: object, a: str) -> str:
    v = getattr(m, a, None)
    return v.strftime("%Y-%m-%d %H:%M") if v else "—"


_META_LABELS: dict[str, str] = {
    "texet_instruction_prompt": "Instruction Prompt",
    "texet_day_identifier": "Day (from prompt)",
    "texet_moderation_source": "Moderation Source",
    "texet_moderation_category": "Moderation Category",
    "texet_moderation_score": "Moderation Score",
    "texet_moderation_notice": "Moderation Notice",
}


def _fmt_meta_detail(m: object, a: str) -> str:
    """Render JSONB meta as labelled key: value lines. No truncation — detail view shows all."""
    v = getattr(m, a, None)
    if not v:
        return "—"
    lines = []
    for k, val in v.items():
        label = _META_LABELS.get(k, k)
        lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _fmt_day(m: object, a: str) -> str:
    v = getattr(m, a, None)
    return str(v) if v is not None else "—"


def _fmt_text_truncated(m: object, a: str) -> str:
    text: str | None = getattr(m, "text", None)
    if text and len(text) > 100:
        return text[:100] + "…"
    return text or "—"


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
    can_export = True
    export_types = ["csv"]
    page_size = 50
    page_size_options = [25, 50, 100]
    column_list = ["id", "created_at"]
    column_details_list = ["id", "created_at", "meta"]
    column_searchable_list = ["id"]
    column_sortable_list = [Speaker.created_at]
    column_default_sort = (Speaker.created_at, True)
    column_formatters = {"created_at": _fmt_dt}  # type: ignore[dict-item]
    column_formatters_detail = {"meta": _fmt_meta_detail}  # type: ignore[dict-item]
    column_labels = {"id": "Speaker ID", "created_at": "Joined"}


class ConversationAdmin(ModelView, model=Conversation):
    name = "Conversation"
    name_plural = "Conversations"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = True
    export_types = ["csv"]
    page_size = 50
    page_size_options = [25, 50, 100]
    column_list = ["owner_speaker_id", "day_identifier", "status", "last_activity_at", "id"]
    column_details_list = [
        "owner_speaker_id",
        "day_identifier",
        "status",
        "last_activity_at",
        "created_at",
        "id",
        "meta",
    ]
    column_searchable_list = ["id", "owner_speaker_id"]
    column_sortable_list = [
        Conversation.last_activity_at,
        Conversation.created_at,
        Conversation.day_identifier,
        Conversation.status,
    ]
    column_default_sort = (Conversation.last_activity_at, True)
    column_filters = [
        AllUniqueStringValuesFilter(Conversation.status, title="Status"),
        OperationColumnFilter(Conversation.day_identifier, title="Day"),
        OperationColumnFilter(Conversation.owner_speaker_id, title="User ID"),
    ]
    column_formatters = {
        "last_activity_at": _fmt_dt,  # type: ignore[dict-item]
        "created_at": _fmt_dt,  # type: ignore[dict-item]
        "day_identifier": _fmt_day,  # type: ignore[dict-item]
    }
    column_formatters_detail = {
        "last_activity_at": _fmt_dt,  # type: ignore[dict-item]
        "created_at": _fmt_dt,  # type: ignore[dict-item]
        "day_identifier": _fmt_day,  # type: ignore[dict-item]
        "meta": _fmt_meta_detail,  # type: ignore[dict-item]
    }
    column_labels = {
        "id": "Conversation ID",
        "owner_speaker_id": "User",
        "day_identifier": "Day",
        "last_activity_at": "Last Active",
        "created_at": "Started",
    }


class UtteranceAdmin(ModelView, model=Utterance):
    name = "Utterance"
    name_plural = "Utterances"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = True
    export_types = ["csv"]
    page_size = 50
    page_size_options = [25, 50, 100]
    column_default_sort = [("timestamp", True), ("id", True)]
    column_list = [
        "speaker_id",
        "conversation_id",
        "timestamp",
        "status",
        "text",
        "error",
    ]
    column_details_list = [
        "speaker_id",
        "conversation_id",
        "reply_to_id",
        "timestamp",
        "status",
        "text",
        "error",
        "created_at",
        "id",
        "meta",
    ]
    column_searchable_list = ["text", "speaker_id", "conversation_id"]
    column_sortable_list = [Utterance.timestamp, Utterance.status, Utterance.created_at]
    column_filters = [
        OperationColumnFilter(Utterance.speaker_id, title="User ID"),
        OperationColumnFilter(Utterance.conversation_id, title="Conversation ID"),
        StaticValuesFilter(
            Utterance.status,
            values=[(status, status) for status in UTTERANCE_STATUSES],
            title="Status",
        ),
    ]
    column_formatters = {
        "timestamp": _fmt_dt,  # type: ignore[dict-item]
        "created_at": _fmt_dt,  # type: ignore[dict-item]
        "text": _fmt_text_truncated,  # type: ignore[dict-item]
    }
    column_formatters_detail = {
        "timestamp": _fmt_dt,  # type: ignore[dict-item]
        "created_at": _fmt_dt,  # type: ignore[dict-item]
        "meta": _fmt_meta_detail,  # type: ignore[dict-item]
    }
    column_labels = {
        "id": "Utterance ID",
        "conversation_id": "Conversation",
        "speaker_id": "User",
        "reply_to_id": "Reply To",
        "timestamp": "Time",
        "created_at": "Created",
        "text": "Message",
        "error": "Error",
        "status": "Status",
    }


class ApiKeyAdmin(ModelView, model=ApiKey):
    name = "API Key"
    name_plural = "API Keys"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    page_size = 50
    column_list = ["id", "name", "key_prefix", "is_active", "created_at", "last_used_at"]
    column_details_list = [
        "id",
        "name",
        "key_prefix",
        "is_active",
        "created_at",
        "last_used_at",
    ]
    column_searchable_list = ["id", "name", "key_prefix"]
    column_labels = {
        "id": "Key ID",
        "name": "Name",
        "key_prefix": "Key Prefix",
        "is_active": "Active",
        "created_at": "Created At",
        "last_used_at": "Last Used At",
    }


class AdminExportAdmin(ModelView, model=AdminExport):
    name = "Admin Export"
    name_plural = "Admin Exports"
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    page_size = 50
    column_list = [
        "id",
        "kind",
        "status",
        "range_start",
        "range_end",
        "utterance_count",
        "conversation_count",
        "speaker_count",
        "verified",
        "created_at",
        "completed_at",
    ]
    column_details_list = [
        "id",
        "kind",
        "status",
        "range_start",
        "range_end",
        "utterance_count",
        "conversation_count",
        "speaker_count",
        "verified",
        "verification_error",
        "sha256",
        "error",
        "created_at",
        "completed_at",
        "meta",
    ]
    column_labels = {
        "id": "Export ID",
        "range_start": "Start",
        "range_end": "End",
        "utterance_count": "Utterances",
        "conversation_count": "Conversations",
        "speaker_count": "Speakers",
        "created_at": "Created At",
        "completed_at": "Completed At",
    }


def init_console(app: FastAPI) -> None:
    if getattr(app.state, "admin_initialized", False):
        return
    if not admin_enabled():
        return

    secret_key = get_admin_secret_key()
    if not secret_key:
        return

    authentication_backend = AdminAuth(secret_key=secret_key)
    if "base_url" in inspect.signature(Admin).parameters:
        admin = Admin(
            app,
            get_engine(),
            authentication_backend=authentication_backend,
            base_url=f"{CONSOLE_PREFIX}/admin",
        )
    else:
        admin = Admin(app, get_engine(), authentication_backend=authentication_backend)
    admin.add_view(SpeakerAdmin)
    admin.add_view(ConversationAdmin)
    admin.add_view(UtteranceAdmin)
    admin.add_view(ApiKeyAdmin)
    admin.add_view(AdminExportAdmin)
    app.state.admin = admin
    app.state.admin_initialized = True
