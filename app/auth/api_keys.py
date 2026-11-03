from __future__ import annotations

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import API_KEY_SCOPE_RESPONSE, hash_api_key
from app.models.auth import ApiKey

API_KEY_PREFIX_LEN = 8


def generate_api_key() -> str:
    return f"texet_{secrets.token_urlsafe(32)}"


async def create_api_key(
    session: AsyncSession, name: str | None = None, scope: str = API_KEY_SCOPE_RESPONSE
) -> str:
    key = generate_api_key()
    session.add(
        ApiKey(
            name=name or None,
            key_hash=hash_api_key(key),
            key_prefix=key[:API_KEY_PREFIX_LEN],
            is_active=True,
            scope=scope,
        )
    )
    await session.flush()
    return key
