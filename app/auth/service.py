import datetime
import hashlib

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMEZONE
from app.db import get_async_session
from app.models.auth import ApiKey

_security = HTTPBearer(auto_error=False)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _get_api_key(session: AsyncSession, token: str) -> ApiKey | None:
    key_hash = hash_api_key(token)
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    try:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = credentials.credentials
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        api_key = await _get_api_key(session, token)
        if api_key:
            api_key.last_used_at = datetime.datetime.now(DEFAULT_TIMEZONE)
            await session.commit()
            return

        has_keys = await session.execute(select(ApiKey.id).limit(1))
        if has_keys.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="API auth is not configured.",
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    finally:
        if session.in_transaction():
            await session.rollback()
