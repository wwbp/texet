"""Run C: weekly summaries for the ten conversation threads.

Uses the app's own summariser, one participant at a time, instead of the
console's force-all button — that button targets every participant active in
the week, which would also sweep up Run A's fifty throwaway users.

Runs inside the api container:

    docker compose ... exec -T api python - < scripts/memory_test/gen_summaries.py
"""

from __future__ import annotations

import asyncio
import datetime

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.response import Utterance
from app.response.crud import bot_speaker_id
from app.summary.service import build_week_transcript, generate_user_weekly_summary

WEEK_START = datetime.date(2026, 8, 2)
USER_IDS = [f"memtest-conv-p{index:02d}" for index in range(1, 11)]


async def main() -> None:
    sessionmaker = get_sessionmaker()
    for user_id in USER_IDS:
        async with sessionmaker() as session:
            result = await session.execute(
                select(Utterance)
                .where(
                    Utterance.speaker_id.in_([user_id, bot_speaker_id(user_id)]),
                    Utterance.timestamp
                    >= datetime.datetime.combine(
                        WEEK_START, datetime.time.min, tzinfo=datetime.UTC
                    ),
                    Utterance.timestamp
                    < datetime.datetime.combine(
                        WEEK_START + datetime.timedelta(days=7),
                        datetime.time.min,
                        tzinfo=datetime.UTC,
                    ),
                )
                .order_by(Utterance.timestamp)
            )
            transcript = build_week_transcript(list(result.scalars().all()), user_id)

        async with sessionmaker() as session:
            generated = await generate_user_weekly_summary(session, user_id, WEEK_START)
        print(
            f"{user_id}: {'summarised' if generated else 'NOTHING TO SUMMARISE'} "
            f"({len(transcript.splitlines())} transcript lines, {len(transcript)} chars)",
            flush=True,
        )


asyncio.run(main())
