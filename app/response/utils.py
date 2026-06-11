from __future__ import annotations

import datetime
from typing import Any


def week_start_utc(dt: datetime.datetime) -> datetime.date:
    """Return the most recent Sunday (UTC) on or before dt."""
    # weekday(): Mon=0 ... Sun=6; days_back maps Sun→0, Mon→1, ..., Sat→6
    days_back = (dt.weekday() + 1) % 7
    return (dt - datetime.timedelta(days=days_back)).date()


def extract_utc_offset(meta: dict[str, Any] | None) -> datetime.timedelta | None:
    """Return the user's UTC offset recorded on an utterance, if any.

    User utterances carry user_local_time directly; bot replies carry the
    triggering request's value inside the texet_generation snapshot.
    """
    if not meta:
        return None
    raw = meta.get("user_local_time")
    if raw is None:
        generation = meta.get("texet_generation")
        if isinstance(generation, dict):
            raw = generation.get("user_local_time")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.utcoffset()


def day_marker(local_date: datetime.date) -> str:
    return f"[{local_date.strftime('%A, %B %-d')}]"
