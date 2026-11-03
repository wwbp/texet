import datetime
import hashlib
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMEZONE
from app.db import get_async_session
from app.models.auth import ApiKey

_security = HTTPBearer(auto_error=False)

# last_used_at is informational (shown in the admin console); refreshing it at
# most once per interval avoids a write + commit on every authenticated request.
_LAST_USED_REFRESH_SECONDS = 60


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _get_api_key(session: AsyncSession, token: str) -> ApiKey | None:
    key_hash = hash_api_key(token)
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    return result.scalar_one_or_none()


API_KEY_SCOPE_RESPONSE = "response"
API_KEY_SCOPE_EXPORT = "export"
API_KEY_SCOPES = (API_KEY_SCOPE_RESPONSE, API_KEY_SCOPE_EXPORT)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_scope(scope: str) -> Callable[..., Awaitable[str]]:
    """Dependency: 401 without a valid active key, 403 if the key lacks `scope`.

    Returns the key id. It is captured before the rollback below expires the
    row, so callers can log it without a lazy load.
    """

    async def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(_security),
        session: AsyncSession = Depends(get_async_session),
    ) -> str:
        try:
            if (
                not credentials
                or credentials.scheme.lower() != "bearer"
                or not credentials.credentials
            ):
                raise _unauthorized()

            api_key = await _get_api_key(session, credentials.credentials)
            if not api_key:
                raise _unauthorized()
            if api_key.scope != scope:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden.")

            key_id = api_key.id
            now = datetime.datetime.now(DEFAULT_TIMEZONE)
            if (
                api_key.last_used_at is None
                or (now - api_key.last_used_at).total_seconds() >= _LAST_USED_REFRESH_SECONDS
            ):
                api_key.last_used_at = now
                await session.commit()
            return key_id
        finally:
            if session.in_transaction():
                await session.rollback()

    return dependency


require_auth = require_scope(API_KEY_SCOPE_RESPONSE)
require_export = require_scope(API_KEY_SCOPE_EXPORT)
