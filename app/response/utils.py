from __future__ import annotations

import datetime
import re
from typing import Any


def week_start_utc(dt: datetime.datetime) -> datetime.date:
    """Return the most recent Sunday (UTC) on or before dt."""
    # weekday(): Mon=0 ... Sun=6; days_back maps Sun→0, Mon→1, ..., Sat→6
    days_back = (dt.weekday() + 1) % 7
    return (dt - datetime.timedelta(days=days_back)).date()


def week_start_for(dt: datetime.datetime, offset: datetime.timedelta | None) -> datetime.date:
    """The most recent Sunday on the participant's clock.

    Weeks used to be UTC for everyone, which put a UTC-5 participant's boundary
    at Saturday 19:00 local: the last five hours of their week were filed under
    the next one, and the summary for that week was generated before those
    hours had happened. Falls back to UTC when no offset is known.
    """
    tz = datetime.timezone(offset) if offset is not None else datetime.UTC
    return week_start_utc(dt.astimezone(tz))


def week_bounds_utc(
    week_start: datetime.date, offset: datetime.timedelta | None
) -> tuple[datetime.datetime, datetime.datetime]:
    """The UTC instants bounding a local week, as [start, end).

    Offsets here are fixed, so seven days is always seven days; consecutive
    weeks meet exactly, leaving a message in one week and one week only.
    """
    tz = datetime.timezone(offset) if offset is not None else datetime.UTC
    start_local = datetime.datetime.combine(week_start, datetime.time.min, tzinfo=tz)
    end_local = start_local + datetime.timedelta(days=7)
    return start_local.astimezone(datetime.UTC), end_local.astimezone(datetime.UTC)


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


# A bracketed segment: opens and closes on one line, with no nesting. The
# character class is the safety property — a '[' that never closes cannot
# swallow the rest of the reply, and a stray ']' cannot pair with a bracket
# from a paragraph above. An unmatched bracket is prose and stays put.
_BRACKETED_SEGMENT = re.compile(r"\[[^\[\]\n]*\]")


def strip_bracketed_segments(text: str | None) -> str | None:
    """Remove '[...]' segments from a bot reply.

    Two artifacts reach participants this way. build_chat_history prepends a
    day_marker — '[Friday, August 21]' — to the first message of each day, and
    the model sometimes reproduces the convention in its own reply. Separately
    it leaks prompt scaffolding: '[Opening message]', or an unfilled slot like
    '[specific area]'. Neither is content, and prod bears that out: of 1272 bot
    utterances, the five carrying a bracketed segment were all one of these,
    and no participant had sent a bracket at all.

    Removal is whitespace-aware, because the token is usually a whole line and
    deleting it alone would leave a blank one behind. Text with no bracketed
    segment is returned unchanged, whitespace included; None passes through so
    callers reading the nullable Utterance.text need no guard.
    """
    if not text or not _BRACKETED_SEGMENT.search(text):
        return text

    kept: list[str] = []
    for line in text.split("\n"):
        pruned = _BRACKETED_SEGMENT.sub("", line)
        if pruned == line:
            kept.append(line)
            continue
        # The line existed only to carry the segment: drop it rather than
        # leaving an orphan blank line where the marker used to be.
        if not pruned.strip():
            continue
        # Removal from mid-line closes the gap it left; a removal at the start
        # of the line takes the indentation it was holding open with it.
        pruned = re.sub(r"[ \t]{2,}", " ", pruned).rstrip()
        if _BRACKETED_SEGMENT.match(line.lstrip()):
            pruned = pruned.lstrip()
        kept.append(pruned)

    return "\n".join(kept).strip()
