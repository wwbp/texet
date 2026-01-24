from __future__ import annotations

import inspect

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqladmin.filters import AllUniqueStringValuesFilter, StaticValuesFilter
from starlette.requests import Request

from app.config import CONSOLE_PREFIX, UTTERANCE_STATUSES, admin_enabled, get_admin_secret_key
from app.console.core import _authorized, _credentials_valid, _now
from app.db import get_engine
from app.models.admin import AdminExport
from app.models.auth import ApiKey
from app.models.response import Conversation, Speaker, Utterance


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


def init_console(app) -> None:
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
