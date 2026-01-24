from __future__ import annotations

import base64
import datetime
import html
import secrets

from fastapi import APIRouter, HTTPException
from starlette.requests import Request

from app.config import (
    CONSOLE_PREFIX,
    DEFAULT_TIMEZONE,
    get_admin_password,
    get_admin_session_ttl_seconds,
    get_admin_username,
)

console_router = APIRouter(prefix=CONSOLE_PREFIX, tags=["console"])


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


def _serialize_datetime(value: datetime.datetime | None) -> str:
    if not value:
        return ""
    return value.isoformat()


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


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
