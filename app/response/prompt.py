from __future__ import annotations

import datetime
import re

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

# Placeholders whose emptiness removes the paragraph they appear in. A section
# the operator wrote for optional context should vanish entirely rather than
# leave a dangling label in front of nothing.
GATING_PLACEHOLDERS = ("base", "daily_content", "weekly_summary", "formatted_time")

# Decorative placeholders render empty without taking their paragraph with
# them — day_suffix is part of a label, not content of its own.
DECORATIVE_PLACEHOLDERS = ("day_suffix",)

TEMPLATE_PLACEHOLDERS = GATING_PLACEHOLDERS + DECORATIVE_PLACEHOLDERS

_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(TEMPLATE_PLACEHOLDERS) + r")\}")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

DEFAULT_INSTRUCTION_TEMPLATE = f"""\
{{base}}

[Today's Activity{{day_suffix}}]
{{daily_content}}

[Previous week summary]
{{weekly_summary}}

[User's Local Time]
The user's current local time is {{formatted_time}}. Use this to inform the tone and relevance \
of your response where appropriate (e.g. time of day, day of week), but do not make it the \
focus of the conversation.

{HISTORY_CONVENTIONS}"""


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


def render_instruction_template(template: str, values: dict[str, str]) -> str:
    """Fill `template`, dropping any paragraph whose gating placeholder is empty.

    Substitution is name-scoped rather than `str.format`, so literal braces in
    operator-authored prose survive and an unknown placeholder stays visible as
    text instead of blanking a section or raising during a live reply.
    """
    paragraphs: list[str] = []
    for raw in _PARAGRAPH_SPLIT_RE.split(template):
        paragraph = raw.strip()
        if not paragraph:
            continue
        names = set(_PLACEHOLDER_RE.findall(paragraph))
        if any(not values.get(name) for name in names if name in GATING_PLACEHOLDERS):
            continue
        paragraphs.append(_PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), paragraph))
    return "\n\n".join(paragraphs)


def compose_instruction_prompt(
    base: str,
    daily_content: str | None = None,
    weekly_summary: str | None = None,
    user_local_time: str | None = None,
    day_number: int | None = None,
    template: str | None = None,
) -> str:
    layout = (template or "").strip() or DEFAULT_INSTRUCTION_TEMPLATE

    formatted_time = ""
    if user_local_time and user_local_time.strip():
        formatted_time = _format_user_local_time(user_local_time.strip()) or ""

    values = {
        "base": base.strip(),
        "day_suffix": f" (Day {day_number})" if day_number is not None else "",
        "daily_content": (daily_content or "").strip(),
        "weekly_summary": (weekly_summary or "").strip(),
        "formatted_time": formatted_time,
    }
    return render_instruction_template(layout, values)
