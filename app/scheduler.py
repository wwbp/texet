from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy import text

from app.db import get_engine, get_sessionmaker
from app.summary.service import run_weekly_summaries

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")

# Postgres advisory-lock key for the weekly-summary job. APScheduler's
# max_instances is per-process only; when the app runs on more than one
# instance (scaled EB env / multiple replicas) every instance would otherwise
# fire this cron. A cluster-wide advisory lock ensures exactly one instance
# runs it, with no per-instance configuration.
_WEEKLY_SUMMARY_LOCK_KEY = 0x7E7E75  # arbitrary, stable


async def _run_weekly_summaries_once() -> None:
    """Run the weekly summary on a single instance, guarded by a Postgres advisory lock."""
    engine = get_engine()
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _WEEKLY_SUMMARY_LOCK_KEY}
            )
        ).scalar()
        if not acquired:
            logger.info("Weekly summary is running on another instance; skipping here.")
            return
        try:
            await run_weekly_summaries(get_sessionmaker())
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _WEEKLY_SUMMARY_LOCK_KEY}
            )


async def _weekly_summary_job() -> None:
    try:
        await _run_weekly_summaries_once()
    except Exception:
        logger.exception("Weekly summary job failed")


def start_scheduler() -> None:
    _scheduler.add_job(
        _weekly_summary_job,
        CronTrigger(day_of_week="sun", hour=0, minute=0, timezone="UTC"),
        max_instances=1,
        id="weekly_summaries",
        replace_existing=True,
    )
    _scheduler.start()


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
