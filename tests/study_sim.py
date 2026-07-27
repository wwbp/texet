"""Synthetic 30-day study corpus for end-to-end prompt-assembly checks.

The reply pipeline derives its context window from ``datetime.now()``: the
current UTC week bounds the chat history, and the week before it selects the
weekly summary. Rather than mock the clock, the seeder anchors the calendar so
that the study day under test *is* today, and backdates everything behind it.
Each checkpoint is therefore an independent scenario running the real code path
against a real wall clock.

Every seeded value carries a unique marker (``DAILY-7``, ``WEEKSUM-2026-07-05``)
so assertions can prove both presence and absence.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import UTTERANCE_STATUS_RECEIVED, UTTERANCE_STATUS_SENT
from app.models.response import DailyPrompt, InstructionTemplate, SystemPrompt
from app.response.crud import (
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    upsert_weekly_summary,
)
from app.response.utils import week_start_utc

STUDY_LENGTH_DAYS = 30

# The hub sends three openings a day; hours are UTC to keep the fixture's
# day-to-week mapping unambiguous.
OPENING_HOURS = (9, 13, 19)

BASE_SYSTEM_PROMPT = "BASE-SYSTEM-PROMPT: you are a supportive study companion."


def daily_marker(day: int) -> str:
    return f"DAILY-{day}"


def daily_content(day: int) -> str:
    return f"{daily_marker(day)}: today's activity for study day {day}."


def week_marker(week_start: datetime.date) -> str:
    return f"WEEKSUM-{week_start.isoformat()}"


def week_summary(week_start: datetime.date) -> str:
    return f"{week_marker(week_start)}: recap of the week beginning {week_start.isoformat()}."


def opening_text(day: int, index: int) -> str:
    return f"OPENING-d{day}-{index}"


def user_text(day: int, index: int) -> str:
    return f"USER-d{day}-{index}"


def bot_reply_text(day: int, index: int) -> str:
    return f"BOTREPLY-d{day}-{index}"


@dataclass(frozen=True)
class StudyCalendar:
    """Maps study days onto real dates so that `study_day` lands on today."""

    study_day: int
    now: datetime.datetime

    @property
    def today(self) -> datetime.date:
        return self.now.date()

    @property
    def start_date(self) -> datetime.date:
        return self.today - datetime.timedelta(days=self.study_day - 1)

    def date_for(self, day: int) -> datetime.date:
        return self.start_date + datetime.timedelta(days=day - 1)

    def at(self, day: int, hour: int, minute: int = 0) -> datetime.datetime:
        return datetime.datetime.combine(
            self.date_for(day), datetime.time(hour, minute), tzinfo=datetime.UTC
        )

    @property
    def current_week_start(self) -> datetime.date:
        return week_start_utc(
            datetime.datetime.combine(self.today, datetime.time(12), tzinfo=datetime.UTC)
        )

    @property
    def previous_week_start(self) -> datetime.date:
        return self.current_week_start - datetime.timedelta(days=7)

    def completed_week_starts(self) -> list[datetime.date]:
        """Every week boundary strictly before the current week, oldest first."""
        first = week_start_utc(
            datetime.datetime.combine(self.start_date, datetime.time(12), tzinfo=datetime.UTC)
        )
        weeks: list[datetime.date] = []
        cursor = first
        while cursor < self.current_week_start:
            weeks.append(cursor)
            cursor += datetime.timedelta(days=7)
        return weeks

    def days_in_current_week(self) -> list[int]:
        return [
            day
            for day in range(1, self.study_day + 1)
            if self.date_for(day) >= self.current_week_start
        ]

    def days_before_current_week(self) -> list[int]:
        return [
            day
            for day in range(1, self.study_day + 1)
            if self.date_for(day) < self.current_week_start
        ]

    def opening_times(self, day: int) -> list[datetime.datetime]:
        """Timestamps for the day's three hub openings.

        Past days use fixed UTC hours. Today's openings are spread across the
        part of the day that has actually elapsed, so they land before the live
        message — an opening timestamped after it would be correctly excluded
        from history and make the fixture, not the system, look broken.
        """
        if day < self.study_day:
            return [self.at(day, hour) for hour in OPENING_HOURS]

        midnight = datetime.datetime.combine(self.today, datetime.time.min, tzinfo=datetime.UTC)
        elapsed = self.now - midnight
        return [midnight + elapsed * fraction for fraction in (0.25, 0.5, 0.75)]


def calendar_for(study_day: int) -> StudyCalendar:
    return StudyCalendar(study_day=study_day, now=datetime.datetime.now(datetime.UTC))


async def seed_prompt_config(session: AsyncSession) -> None:
    """Seed the three DB-backed prompt sources the pipeline reads."""
    session.add(SystemPrompt(prompt=BASE_SYSTEM_PROMPT))
    # The built-in default template, pinned explicitly so a later default
    # change surfaces here as a failure rather than silently altering the study.
    session.add(
        InstructionTemplate(
            template=(
                "{base}\n\n"
                "[Today's Activity{day_suffix}]\n"
                "{daily_content}\n\n"
                "[Previous week summary]\n"
                "{weekly_summary}\n\n"
                "[User's Local Time]\n"
                "The user's current local time is {formatted_time}."
            )
        )
    )
    for day in range(1, STUDY_LENGTH_DAYS + 1):
        session.add(DailyPrompt(day_number=day, content=daily_content(day)))
    await session.flush()


async def seed_weekly_summaries(
    session: AsyncSession, user_id: str, calendar: StudyCalendar
) -> list[datetime.date]:
    """Write a distinctly-labelled summary for every completed week."""
    weeks = calendar.completed_week_starts()
    for week_start in weeks:
        await upsert_weekly_summary(session, user_id, week_start, week_summary(week_start))
    return weeks


async def seed_conversation(
    session: AsyncSession,
    user_id: str,
    calendar: StudyCalendar,
    *,
    reply_hours: tuple[int, ...] = (10, 20),
) -> None:
    """Replay days 1..study_day: three openings a day plus user/bot turns.

    The final day is seeded up to its openings only — the live message under
    test is the user's reply to them.
    """
    speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
    bot = await get_or_create_bot_speaker(session, user_id)
    conversation = await get_or_create_conversation(session, speaker.id)

    for day in range(1, calendar.study_day + 1):
        local_time = calendar.at(day, 12).isoformat()

        for index, timestamp in enumerate(calendar.opening_times(day), start=1):
            opening = await create_utterance(
                session,
                conversation.id,
                bot.id,
                opening_text(day, index),
                meta={"day_number": day, "is_initial": True, "texet_hub_initial": True},
                status=UTTERANCE_STATUS_SENT,
            )
            opening.timestamp = timestamp

        # The day under test gets no user turns; its reply is the live one.
        if day == calendar.study_day:
            continue

        for index, hour in enumerate(reply_hours, start=1):
            turn = await create_utterance(
                session,
                conversation.id,
                speaker.id,
                user_text(day, index),
                meta={"day_number": day, "user_local_time": local_time},
                status=UTTERANCE_STATUS_RECEIVED,
            )
            turn.timestamp = calendar.at(day, hour, minute=5)

            reply = await create_utterance(
                session,
                conversation.id,
                bot.id,
                bot_reply_text(day, index),
                meta={"day_number": day},
                status=UTTERANCE_STATUS_SENT,
            )
            reply.timestamp = calendar.at(day, hour, minute=6)

    await session.flush()
