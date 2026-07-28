"""Print a per-reply prompt timeline for one participant.

Every bot reply persists a texet_generation snapshot: the exact system prompt
and chat history handed to the LLM. This walks a participant's replies in order
and shows what fed each turn, then cross-checks each snapshot against the
prompt rows currently in the database:

  - was a daily prompt expected for the reported day_number, and did it land?
  - did a previous-week summary exist, and did it land?

Mismatches are what you want to catch during a live study; a silently missing
daily prompt looks identical to a day with no prompt configured.

Read-only.

Usage:
    uv run python scripts/prompt_trace.py --user-id u1
    uv run python scripts/prompt_trace.py --user-id u1 --full --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import re
from typing import Any

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.response import Utterance
from app.response.crud import bot_speaker_id, get_daily_prompt, get_weekly_summary

_BLANK_LINE = re.compile(r"\n\s*\n")

OK = "ok"
MISSING = "MISSING"
NOT_EXPECTED = "n/a"


def _truncate(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _paragraphs(system_prompt: str) -> list[str]:
    return [p.strip() for p in _BLANK_LINE.split(system_prompt) if p.strip()]


async def _check_daily(
    session: Any, snapshot: dict[str, Any], system_prompt: str
) -> tuple[str, str]:
    day_number = snapshot.get("day_number")
    if day_number is None:
        return NOT_EXPECTED, "no day_number on the request"
    prompt = await get_daily_prompt(session, day_number)
    if prompt is None:
        return NOT_EXPECTED, f"no daily prompt configured for day {day_number}"
    if prompt.content.strip() in system_prompt:
        return OK, _truncate(prompt.content, 60)
    return MISSING, f"day {day_number} prompt configured but absent from the prompt"


async def _check_summary(
    session: Any, user_id: str, snapshot: dict[str, Any], system_prompt: str
) -> tuple[str, str]:
    raw_week = snapshot.get("week_start")
    if not isinstance(raw_week, str):
        return NOT_EXPECTED, "no week_start on the snapshot"
    prev_week = datetime.date.fromisoformat(raw_week) - datetime.timedelta(days=7)
    summary = await get_weekly_summary(session, user_id, prev_week)
    if summary is None:
        return NOT_EXPECTED, f"no summary stored for week of {prev_week}"
    if summary.strip() in system_prompt:
        return OK, f"week of {prev_week}: {_truncate(summary, 50)}"
    return MISSING, f"summary for week of {prev_week} exists but is absent from the prompt"


async def trace(user_id: str, limit: int, full: bool) -> int:
    bot_id = bot_speaker_id(user_id)
    sessionmaker = get_sessionmaker()
    problems = 0

    async with sessionmaker() as session:
        result = await session.execute(
            select(Utterance)
            .where(Utterance.speaker_id == bot_id)
            .order_by(Utterance.timestamp.desc())
            .limit(limit)
        )
        replies = [u for u in result.scalars().all() if u.meta and u.meta.get("texet_generation")]
        replies.reverse()

        if not replies:
            print(f"No traced replies for user {user_id!r}.")
            print("(Openings recorded with is_initial carry no snapshot — they skip the LLM.)")
            return 0

        print(f"Prompt trace for {user_id} — {len(replies)} replies\n")

        for reply in replies:
            snapshot = reply.meta["texet_generation"]
            system_prompt = str(snapshot.get("system_prompt", ""))
            history = snapshot.get("chat_history", []) or []

            stamp = reply.timestamp.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            print("─" * 78)
            print(f"{stamp}  reply {reply.id}  [{reply.status}]")
            print(
                f"  day_number={snapshot.get('day_number')}  "
                f"week_start={snapshot.get('week_start')}  "
                f"engine={snapshot.get('provider')}/{snapshot.get('model_id')}"
            )
            print(f"  query: {_truncate(str(snapshot.get('query', '')), 66)}")

            daily_status, daily_note = await _check_daily(session, snapshot, system_prompt)
            summary_status, summary_note = await _check_summary(
                session, user_id, snapshot, system_prompt
            )
            for label, status, note in (
                ("daily prompt", daily_status, daily_note),
                ("week summary", summary_status, summary_note),
            ):
                print(f"  {label:<13} [{status:^7}] {note}")
                if status == MISSING:
                    problems += 1

            print(f"  system prompt: {len(system_prompt)} chars")
            for index, paragraph in enumerate(_paragraphs(system_prompt), start=1):
                print(f"    {index}. {_truncate(paragraph, 68)}")

            roles = [turn.get("role") for turn in history]
            print(f"  history: {len(history)} turns {roles}")

            if full:
                print("\n  ── system prompt ──")
                for line in system_prompt.splitlines():
                    print(f"  | {line}")
                print("\n  ── chat history ──")
                for turn in history:
                    for line in str(turn.get("content", "")).splitlines():
                        print(f"  | {turn.get('role'):>9}: {line}")
                print()

        print("─" * 78)
        if problems:
            print(f"{problems} mismatch(es): a configured prompt did not reach the model.")
        else:
            print("All configured prompts reached the model.")

    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Participant speaker id")
    parser.add_argument("--limit", type=int, default=20, help="Most recent replies (default 20)")
    parser.add_argument("--full", action="store_true", help="Dump full prompt and history")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(trace(args.user_id, args.limit, args.full)))


if __name__ == "__main__":
    main()
