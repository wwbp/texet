import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMEZONE, DEFAULT_TIMEZONE_NAME


def test_default_timezone_is_est() -> None:
    assert DEFAULT_TIMEZONE_NAME == "EST"
    assert DEFAULT_TIMEZONE.utcoffset(None) == datetime.timedelta(hours=-5)


def test_now_uses_est_offset() -> None:
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(hours=-5)


@pytest.mark.asyncio
async def test_db_session_timezone_is_est(async_session: AsyncSession) -> None:
    result = await async_session.execute(text("SELECT EXTRACT(TIMEZONE FROM now())"))
    assert int(result.scalar_one()) == -18000
