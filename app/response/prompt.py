from __future__ import annotations

import datetime


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
    opening_message: str | None = None,
    user_local_time: str | None = None,
) -> str:
    parts = [base.strip()]
    if opening_message and opening_message.strip():
        parts.append(f"[Opening message]\n{opening_message.strip()}")
    if daily_content and daily_content.strip():
        parts.append(f"[Daily Activity]\n{daily_content.strip()}")
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
    return "\n\n".join(parts)
