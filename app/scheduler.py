from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from app.db import get_sessionmaker
from app.summary.service import run_weekly_summaries

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")


async def _weekly_summary_job() -> None:
    try:
        await run_weekly_summaries(get_sessionmaker())
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
