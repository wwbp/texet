import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.response import Speaker
from app.response.crud import bot_speaker_id, get_or_create_bot_speaker, get_or_create_speaker


@pytest.mark.asyncio
async def test_get_or_create_speaker(async_session: AsyncSession) -> None:
    speaker = await get_or_create_speaker(async_session, "user-1", meta={"type": "user"})
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


def test_bot_speaker_id_hashes_long_user_id() -> None:
    user_id = "u" * 128
    bot_id = bot_speaker_id(user_id)
    assert bot_id.startswith("bot:")
    assert len(bot_id) <= 128
    assert bot_id != f"bot:{user_id}"
