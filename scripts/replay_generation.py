"""Replay a stored generation snapshot against the current context pipeline.

Loads a bot utterance's texet_generation snapshot (the exact system prompt and
chat history that produced the reply), recomputes both with the code as it is
now, and prints unified diffs. Read-only; useful for eyeballing how a context
change would have altered a real prod generation.

Usage:
    uv run python scripts/replay_generation.py <bot_utterance_id_or_prefix>
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import sys

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.response import Utterance
from app.response.crud import (
    build_chat_history,
    get_daily_prompt,
    get_instruction_template,
    get_or_create_system_prompt,
    get_weekly_summary,
)
from app.response.prompt import compose_instruction_prompt


def _render_history(history: list[dict[str, str]]) -> list[str]:
    return [f"{turn['role']}: {turn['content']}" for turn in history]


def _print_diff(title: str, old: list[str], new: list[str]) -> None:
    print(f"\n=== {title} ===")
    diff = list(difflib.unified_diff(old, new, fromfile="snapshot", tofile="current", lineterm=""))
    if diff:
        print("\n".join(diff))
    else:
        print("(identical)")


async def replay(utterance_id_prefix: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(Utterance).where(Utterance.id.like(f"{utterance_id_prefix}%"))
        )
        candidates = [
            u for u in result.scalars().all() if u.meta and u.meta.get("texet_generation")
        ]
        if not candidates:
            raise SystemExit(
                f"No bot utterance with a texet_generation snapshot matches "
                f"'{utterance_id_prefix}'."
            )
        if len(candidates) > 1:
            ids = ", ".join(u.id for u in candidates)
            raise SystemExit(f"Prefix is ambiguous, matches: {ids}")
        bot_utt = candidates[0]
        snapshot = bot_utt.meta["texet_generation"]

        if not bot_utt.reply_to_id:
            raise SystemExit(
                "Bot utterance has no reply_to_id; cannot locate the triggering message."
            )
        user_utt = await session.get(Utterance, bot_utt.reply_to_id)
        if user_utt is None:
            raise SystemExit("Triggering user utterance not found.")

        week_start = datetime.date.fromisoformat(snapshot["week_start"])
        week_start_dt = datetime.datetime.combine(
            week_start, datetime.time.min, tzinfo=datetime.UTC
        )
        day_number = snapshot.get("day_number")
        user_local_time = snapshot.get("user_local_time")
        user_id = user_utt.speaker_id

        new_history_msgs = await build_chat_history(
            session,
            conversation_id=user_utt.conversation_id,
            user_id=user_id,
            up_to_timestamp=user_utt.timestamp,
            exclude_utterance_id=user_utt.id,
            since_timestamp=week_start_dt,
            annotate_days=True,
        )
        new_history = [{"role": m.role.value, "content": m.content} for m in new_history_msgs]

        base_prompt = await get_or_create_system_prompt(session)
        daily_prompt = (
            await get_daily_prompt(session, day_number) if day_number is not None else None
        )
        prev_summary = await get_weekly_summary(
            session, user_id, week_start - datetime.timedelta(days=7)
        )
        instruction_template = await get_instruction_template(session)
        new_system_prompt = compose_instruction_prompt(
            base=base_prompt,
            daily_content=daily_prompt.content if daily_prompt else None,
            weekly_summary=prev_summary,
            user_local_time=user_local_time,
            day_number=day_number,
            template=instruction_template,
        )

    print(f"Bot utterance: {bot_utt.id}")
    print(
        f"Snapshot version: {snapshot.get('version')}  "
        f"provider: {snapshot.get('provider')}  model: {snapshot.get('model_id')}"
    )
    print(f"Query: {snapshot.get('query')}")
    _print_diff(
        "System prompt (snapshot vs current code)",
        str(snapshot.get("system_prompt", "")).splitlines(),
        new_system_prompt.splitlines(),
    )
    _print_diff(
        "Chat history (snapshot vs current code)",
        _render_history(snapshot.get("chat_history", [])),
        _render_history(new_history),
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    asyncio.run(replay(sys.argv[1]))


if __name__ == "__main__":
    main()
