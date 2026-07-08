from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app import scheduler


def _engine_from(session: AsyncSession) -> object:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    return bind.engine if isinstance(bind, AsyncConnection) else bind


@pytest.mark.asyncio
async def test_weekly_summary_runs_on_only_one_instance(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent runs (simulating two instances) — advisory lock lets only one execute."""
    engine = _engine_from(async_session)
    monkeypatch.setattr(scheduler, "get_engine", lambda: engine)

    calls = 0

    async def _fake_run(_sessionmaker: object) -> None:
        nonlocal calls
        calls += 1
        # Hold the lock long enough that the concurrent attempt sees it taken.
        await asyncio.sleep(0.3)

    monkeypatch.setattr(scheduler, "run_weekly_summaries", _fake_run)

    await asyncio.gather(
        scheduler._run_weekly_summaries_once(),
        scheduler._run_weekly_summaries_once(),
    )

    assert calls == 1


@pytest.mark.asyncio
async def test_lock_is_released_for_the_next_run(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run releases the advisory lock so a later run can acquire it again."""
    engine = _engine_from(async_session)
    monkeypatch.setattr(scheduler, "get_engine", lambda: engine)

    calls = 0

    async def _fake_run(_sessionmaker: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(scheduler, "run_weekly_summaries", _fake_run)

    await scheduler._run_weekly_summaries_once()
    await scheduler._run_weekly_summaries_once()

    assert calls == 2
