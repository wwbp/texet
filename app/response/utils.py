from __future__ import annotations

import datetime


def week_start_utc(dt: datetime.datetime) -> datetime.date:
    """Return the most recent Sunday (UTC) on or before dt."""
    # weekday(): Mon=0 ... Sun=6; days_back maps Sun→0, Mon→1, ..., Sat→6
    days_back = (dt.weekday() + 1) % 7
    return (dt - datetime.timedelta(days=days_back)).date()
