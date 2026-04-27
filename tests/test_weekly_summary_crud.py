from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.response.crud import (
    get_or_create_speaker,
    get_weekly_summary,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc

# ---------------------------------------------------------------------------
# week_start_utc — pure function, no DB needed
# ---------------------------------------------------------------------------

_SUNDAY = datetime.date(2026, 4, 12)  # known Sunday


@pytest.mark.parametrize(
    "dt,expected",
    [
        # Sunday itself → same day
        (datetime.datetime(2026, 4, 12, 0, 0, 0, tzinfo=datetime.UTC), datetime.date(2026, 4, 12)),
        # Sunday at end of day
        (
            datetime.datetime(2026, 4, 12, 23, 59, 59, tzinfo=datetime.UTC),
            datetime.date(2026, 4, 12),
        ),
        # Monday → back to Sunday
        (datetime.datetime(2026, 4, 13, 8, 0, 0, tzinfo=datetime.UTC), datetime.date(2026, 4, 12)),
        # Wednesday mid-week
        (datetime.datetime(2026, 4, 15, 12, 0, 0, tzinfo=datetime.UTC), datetime.date(2026, 4, 12)),
        # Saturday → back to Sunday
        (
            datetime.datetime(2026, 4, 18, 23, 59, 0, tzinfo=datetime.UTC),
            datetime.date(2026, 4, 12),
        ),
        # Next Sunday → new week
        (datetime.datetime(2026, 4, 19, 0, 0, 0, tzinfo=datetime.UTC), datetime.date(2026, 4, 19)),
    ],
)
def test_week_start_utc(dt: datetime.datetime, expected: datetime.date) -> None:
    assert week_start_utc(dt) == expected


# ---------------------------------------------------------------------------
# get_weekly_summary / upsert_weekly_summary — require DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_weekly_summary_returns_none_when_missing(
    async_session: AsyncSession,
) -> None:
    async with async_session.begin():
        await get_or_create_speaker(async_session, "u-ws-missing", meta={"type": "user"})

    result = await get_weekly_summary(async_session, "u-ws-missing", _SUNDAY)
    assert result is None


@pytest.mark.asyncio
async def test_upsert_weekly_summary_creates_and_retrieves(
    async_session: AsyncSession,
) -> None:
    async with async_session.begin():
        await get_or_create_speaker(async_session, "u-ws-create", meta={"type": "user"})

    async with async_session.begin():
        await upsert_weekly_summary(async_session, "u-ws-create", _SUNDAY, "first summary")

    result = await get_weekly_summary(async_session, "u-ws-create", _SUNDAY)
    assert result == "first summary"


@pytest.mark.asyncio
async def test_upsert_weekly_summary_is_idempotent(
    async_session: AsyncSession,
) -> None:
    async with async_session.begin():
        await get_or_create_speaker(async_session, "u-ws-idem", meta={"type": "user"})

    async with async_session.begin():
        await upsert_weekly_summary(async_session, "u-ws-idem", _SUNDAY, "first summary")

    async with async_session.begin():
        await upsert_weekly_summary(async_session, "u-ws-idem", _SUNDAY, "updated summary")

    result = await get_weekly_summary(async_session, "u-ws-idem", _SUNDAY)
    assert result == "updated summary"


@pytest.mark.asyncio
async def test_upsert_weekly_summary_different_weeks_are_independent(
    async_session: AsyncSession,
) -> None:
    week_a = datetime.date(2026, 4, 5)
    week_b = datetime.date(2026, 4, 12)

    async with async_session.begin():
        await get_or_create_speaker(async_session, "u-ws-weeks", meta={"type": "user"})

    async with async_session.begin():
        await upsert_weekly_summary(async_session, "u-ws-weeks", week_a, "week a")
        await upsert_weekly_summary(async_session, "u-ws-weeks", week_b, "week b")

    assert await get_weekly_summary(async_session, "u-ws-weeks", week_a) == "week a"
    assert await get_weekly_summary(async_session, "u-ws-weeks", week_b) == "week b"
