"""End-to-end sanity check for prompt assembly across a 30-day study arc.

Drives the real reply pipeline (no stubbed `_generate_reply`) with external
APIs mocked, then asserts on the `texet_generation` snapshot the pipeline
persists onto every bot reply. The snapshot records the exact system prompt and
chat history handed to the LLM, so it is the trace point for "did the model get
what it should have".

The study runs 30 days; the hub sends three openings a day and the participant
replies in between. Each test anchors a synthetic corpus so that the study day
under test is today. See `tests/study_sim.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.response import Utterance
from app.response import service as response_service
from app.response.schemas import ChatRequest
from app.response.utils import day_marker
from tests.study_sim import (
    BASE_SYSTEM_PROMPT,
    OPENING_HOURS,
    StudyCalendar,
    bot_reply_text,
    calendar_for,
    daily_marker,
    opening_text,
    seed_conversation,
    seed_prompt_config,
    seed_weekly_summaries,
    user_text,
    week_marker,
)


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def mocked_externals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real pipeline with LLM, moderation, and SMS mocked and instant."""
    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "true")
    monkeypatch.setenv("MOCK_LLM_LATENCY_MS", "0")
    monkeypatch.setenv("MOCK_MODERATION_LATENCY_MS", "0")
    monkeypatch.setenv("MOCK_SMS_LATENCY_MS", "0")


async def _run_study_day(
    session: AsyncSession, study_day: int
) -> tuple[dict[str, Any], StudyCalendar]:
    """Seed days 1..study_day, then send the live reply for today.

    Returns the generation snapshot recorded for that reply.
    """
    user_id = f"study-d{study_day}"
    calendar = calendar_for(study_day)

    async with session.begin():
        await seed_prompt_config(session)
        await seed_conversation(session, user_id, calendar)
        await seed_weekly_summaries(session, user_id, calendar)

    local_time = calendar.at(study_day, 20).isoformat()
    queued = await response_service.process_chat(
        session,
        ChatRequest(user_id=user_id, message=user_text(study_day, 1)),
        meta={"day_number": study_day, "user_local_time": local_time},
    )

    sessionmaker = _sessionmaker_from(session)
    await response_service._run_deferred_reply(
        user_id, queued.user_utterance_id, queued.reply_utterance_id, sessionmaker
    )

    session.expire_all()
    reply = await session.get(Utterance, queued.reply_utterance_id)
    assert reply is not None, "bot reply row disappeared"
    assert reply.meta is not None, "bot reply carries no meta"
    snapshot = reply.meta.get("texet_generation")
    assert isinstance(snapshot, dict), f"no generation snapshot recorded: {reply.meta}"
    return snapshot, calendar


def _history_text(snapshot: dict[str, Any]) -> str:
    return "\n".join(entry["content"] for entry in snapshot["chat_history"])


# ---------------------------------------------------------------------------
# Day 1 — cold start: no history, no summary, first daily prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_one_has_daily_prompt_but_no_summary(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    snapshot, _ = await _run_study_day(async_session, 1)
    system_prompt = snapshot["system_prompt"]

    assert BASE_SYSTEM_PROMPT in system_prompt
    assert "[Today's Activity (Day 1)]" in system_prompt
    assert daily_marker(1) in system_prompt
    # No week has completed, so the summary paragraph must be gone entirely.
    assert "[Previous week summary]" not in system_prompt
    assert "WEEKSUM-" not in system_prompt

    # Only today's three openings precede the live message.
    assert [e["role"] for e in snapshot["chat_history"]] == ["assistant"] * len(OPENING_HOURS)


@pytest.mark.asyncio
async def test_day_one_reports_day_number_and_local_time(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    snapshot, calendar = await _run_study_day(async_session, 1)

    assert snapshot["day_number"] == 1
    assert snapshot["week_start"] == calendar.current_week_start.isoformat()
    assert "[User's Local Time]" in snapshot["system_prompt"]


# ---------------------------------------------------------------------------
# Day 2 — history has accumulated, still no completed week
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_two_carries_yesterday_when_same_week(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    snapshot, calendar = await _run_study_day(async_session, 2)
    history = _history_text(snapshot)

    assert daily_marker(2) in snapshot["system_prompt"]
    assert daily_marker(1) not in snapshot["system_prompt"], "stale daily prompt leaked"

    # Day 1 is only in history if it fell inside the current UTC week.
    if 1 in calendar.days_in_current_week():
        assert user_text(1, 1) in history
        assert bot_reply_text(1, 1) in history
    else:
        assert user_text(1, 1) not in history


# ---------------------------------------------------------------------------
# The two-week context horizon — the study's defining constraint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("study_day", [8, 15, 30])
@pytest.mark.asyncio
async def test_only_the_previous_week_summary_is_injected(
    async_session: AsyncSession, mocked_externals: None, study_day: int
) -> None:
    snapshot, calendar = await _run_study_day(async_session, study_day)
    system_prompt = snapshot["system_prompt"]

    completed = calendar.completed_week_starts()
    assert completed, f"day {study_day} should follow at least one completed week"

    assert week_marker(calendar.previous_week_start) in system_prompt

    # Every earlier week is provably absent: history spans the current week and
    # the summary covers only the week before it, so anything older is lost.
    for week_start in completed:
        if week_start == calendar.previous_week_start:
            continue
        assert week_marker(week_start) not in system_prompt, (
            f"summary for {week_start} leaked into day {study_day}"
        )


@pytest.mark.parametrize("study_day", [8, 15, 30])
@pytest.mark.asyncio
async def test_history_is_confined_to_the_current_week(
    async_session: AsyncSession, mocked_externals: None, study_day: int
) -> None:
    snapshot, calendar = await _run_study_day(async_session, study_day)
    history = _history_text(snapshot)

    for day in calendar.days_in_current_week():
        assert opening_text(day, 1) in history, f"day {day} opening missing from history"

    for day in calendar.days_before_current_week():
        assert opening_text(day, 1) not in history, f"day {day} leaked past the week boundary"
        assert user_text(day, 1) not in history, f"day {day} leaked past the week boundary"


@pytest.mark.asyncio
async def test_day_thirty_retains_nothing_from_weeks_one_to_three(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    """Pin the documented memory span: by day 30 only week 4 + this week survive."""
    snapshot, calendar = await _run_study_day(async_session, 30)
    blob = snapshot["system_prompt"] + "\n" + _history_text(snapshot)

    survivors = set(calendar.days_in_current_week())
    for day in range(1, 31):
        if day in survivors:
            continue
        assert user_text(day, 1) not in blob, f"day {day} unexpectedly survived to day 30"


# ---------------------------------------------------------------------------
# Daily prompt selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("study_day", [1, 7, 8, 30])
@pytest.mark.asyncio
async def test_daily_prompt_matches_the_reported_day_number(
    async_session: AsyncSession, mocked_externals: None, study_day: int
) -> None:
    snapshot, _ = await _run_study_day(async_session, study_day)
    system_prompt = snapshot["system_prompt"]

    assert f"[Today's Activity (Day {study_day})]" in system_prompt
    assert daily_marker(study_day) in system_prompt

    for other in (1, 7, 8, 30):
        if other == study_day:
            continue
        assert f"{daily_marker(other)}:" not in system_prompt


# ---------------------------------------------------------------------------
# Three openings a day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_openings_reach_the_model(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    snapshot, _ = await _run_study_day(async_session, 7)
    history = _history_text(snapshot)

    for index in range(1, len(OPENING_HOURS) + 1):
        assert opening_text(7, index) in history


@pytest.mark.asyncio
async def test_unanswered_openings_arrive_as_consecutive_assistant_turns(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    """Today's openings are unanswered, so the model sees a bot-only run."""
    snapshot, _ = await _run_study_day(async_session, 7)
    roles = [entry["role"] for entry in snapshot["chat_history"]]

    assert roles[-len(OPENING_HOURS) :] == ["assistant"] * len(OPENING_HOURS)


@pytest.mark.asyncio
async def test_history_is_day_marked_and_chronological(
    async_session: AsyncSession, mocked_externals: None
) -> None:
    snapshot, calendar = await _run_study_day(async_session, 7)
    contents = [entry["content"] for entry in snapshot["chat_history"]]

    for day in calendar.days_in_current_week():
        marker = day_marker(calendar.date_for(day))
        assert any(marker in c for c in contents), f"missing day marker for study day {day}"

    positions = [
        next(i for i, c in enumerate(contents) if opening_text(day, 1) in c)
        for day in calendar.days_in_current_week()
    ]
    assert positions == sorted(positions), "history is out of chronological order"
