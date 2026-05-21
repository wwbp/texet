from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.response import DailyPrompt
from app.response.crud import get_daily_prompt


@pytest.mark.asyncio
async def test_get_daily_prompt_returns_none_when_missing(async_session: AsyncSession) -> None:
    result = await get_daily_prompt(async_session, 99)
    assert result is None


@pytest.mark.asyncio
async def test_get_daily_prompt_returns_matching_row(async_session: AsyncSession) -> None:
    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=5, content="Day 5 content."))

    result = await get_daily_prompt(async_session, 5)
    assert result is not None
    assert result.day_number == 5
    assert result.content == "Day 5 content."


@pytest.mark.asyncio
async def test_get_daily_prompt_wrong_identifier_returns_none(async_session: AsyncSession) -> None:
    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=3, content="Day 3."))

    result = await get_daily_prompt(async_session, 4)
    assert result is None


@pytest.mark.asyncio
async def test_day_number_is_unique(async_session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=7, content="First."))

    with pytest.raises(IntegrityError):
        async with async_session.begin():
            async_session.add(DailyPrompt(day_number=7, content="Duplicate."))
