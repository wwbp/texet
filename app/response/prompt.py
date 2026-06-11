from __future__ import annotations

import datetime

HISTORY_CONVENTIONS = """\
[Conversation history conventions]
The conversation above is the user's actual SMS thread with you since Sunday — it is real, \
and you do remember it. Lines like [Tuesday, June 9] mark where a new day begins; the thread \
spans multiple days. The daily opening texts you sent appear as your own messages. A \
[Previous week summary] section, when present, summarizes older conversations. Messages \
withheld by safety filters are not visible to you. Time or day references inside older \
messages may be stale — trust [User's Local Time] for the current moment. A \
[start of conversation] placeholder may appear as the first user turn; it is not a real \
message."""


def _format_user_local_time(iso_str: str) -> str | None:
    """Parse an ISO 8601 datetime string and return a human-readable label."""
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    day_name = dt.strftime("%A")
    date_str = dt.strftime("%B %-d, %Y")
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    if dt.tzinfo is not None:
        offset = dt.utcoffset()
        assert offset is not None
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        abs_minutes = abs(total_minutes)
        h, m = divmod(abs_minutes, 60)
        tz_label = f"UTC{sign}{h}" if m == 0 else f"UTC{sign}{h}:{m:02d}"
    else:
        tz_label = "UTC offset unknown"
    return f"{day_name}, {date_str} at {time_str} ({tz_label})"


def compose_instruction_prompt(
    base: str,
    daily_content: str | None = None,
    weekly_summary: str | None = None,
    user_local_time: str | None = None,
    day_number: int | None = None,
) -> str:
    parts = [base.strip()]
    if daily_content and daily_content.strip():
        label = (
            f"[Today's Activity (Day {day_number})]"
            if day_number is not None
            else "[Today's Activity]"
        )
        parts.append(f"{label}\n{daily_content.strip()}")
    if weekly_summary and weekly_summary.strip():
        parts.append(f"[Previous week summary]\n{weekly_summary.strip()}")
    if user_local_time and user_local_time.strip():
        formatted = _format_user_local_time(user_local_time.strip())
        if formatted:
            parts.append(
                f"[User's Local Time]\n"
                f"The user's current local time is {formatted}. "
                f"Use this to inform the tone and relevance of your response where appropriate "
                f"(e.g. time of day, day of week), but do not make it the focus of the conversation."
            )
    parts.append(HISTORY_CONVENTIONS)
    return "\n\n".join(parts)
