import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_ops import bot_speaker_id, get_or_create_bot_speaker, get_or_create_speaker
from app.models import Speaker


@pytest.mark.asyncio
async def test_get_or_create_speaker(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(
        async_session, "user-1", meta={"type": "user"}
    )
    await async_session.commit()

    fetched = await async_session.get(Speaker, "user-1")
    assert fetched is not None
    assert fetched.id == speaker.id
    assert fetched.meta == {"type": "user"}

    await get_or_create_speaker(async_session, "user-1")
    await async_session.commit()
    count = await async_session.execute(select(func.count()).select_from(Speaker))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_get_or_create_bot_speaker(async_session: AsyncSession) -> None:
    bot = await get_or_create_bot_speaker(async_session, "user-1")
    await async_session.commit()

    fetched = await async_session.get(Speaker, bot_speaker_id("user-1"))
    assert fetched is not None
    assert fetched.id == bot.id
    assert fetched.meta == {"type": "bot"}
