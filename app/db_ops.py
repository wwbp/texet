from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Speaker


def bot_speaker_id(user_id: str) -> str:
    return f"bot:{user_id}"


async def get_or_create_speaker(
    session: AsyncSession,
    speaker_id: str,
    meta: dict[str, Any] | None = None,
) -> Speaker:
    speaker = await session.get(Speaker, speaker_id)
    if speaker:
        return speaker

    speaker = Speaker(id=speaker_id, meta=meta)
    session.add(speaker)
    await session.flush()
    return speaker


async def get_or_create_bot_speaker(session: AsyncSession, user_id: str) -> Speaker:
    return await get_or_create_speaker(session, bot_speaker_id(user_id), meta={"type": "bot"})
