"""day_number problems and missing daily prompts must be visible, not silent.

The pipeline reads day_number straight from request metadata. Anything that
isn't an int used to fall through to None, dropping the daily activity section
while the reply still went out looking perfectly normal — so a hub quoting the
field ("7") could cost a 30-day study every one of its daily prompts without a
single error anywhere.

Numeric strings are now coerced so data is not lost, but every deviation is
recorded as a PromptIssue for the console.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.response import DailyPrompt, PromptIssue
from app.response import service as response_service
from app.response.crud import (
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.response.service import (
    PROMPT_ISSUE_DAILY_PROMPT_MISSING,
    PROMPT_ISSUE_DAY_NUMBER_INVALID,
    _coerce_day_number,
)


def _sessionmaker_from(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        raise RuntimeError("AsyncSession missing bind.")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# _coerce_day_number — pure
# ---------------------------------------------------------------------------


def test_int_passes_through_without_complaint() -> None:
    assert _coerce_day_number(7) == (7, None)


def test_absent_day_number_is_not_an_issue() -> None:
    assert _coerce_day_number(None) == (None, None)


def test_numeric_string_is_coerced_but_reported() -> None:
    day, problem = _coerce_day_number("7")
    assert day == 7
    assert problem is not None and "string" in problem


def test_whitespace_padded_numeric_string_is_coerced() -> None:
    day, problem = _coerce_day_number("  12 ")
    assert day == 12
    assert problem is not None


def test_boolean_is_rejected() -> None:
    """isinstance(True, int) is True in Python, so bools need an explicit guard."""
    day, problem = _coerce_day_number(True)
    assert day is None
    assert problem is not None and "boolean" in problem


def test_non_numeric_string_is_rejected() -> None:
    day, problem = _coerce_day_number("day seven")
    assert day is None
    assert problem is not None


def test_float_is_rejected() -> None:
    day, problem = _coerce_day_number(7.5)
    assert day is None
    assert problem is not None


# ---------------------------------------------------------------------------
# Recorded through the live pipeline
# ---------------------------------------------------------------------------


async def _reply_with_metadata(
    session: AsyncSession, user_id: str, meta: dict[str, object]
) -> None:
    async def _allow(*_a: object, **_k: object) -> tuple[bool, str, str, float]:
        return False, "", "", 0.0

    async def _reply(*_a: object, **_k: object) -> str:
        return "ok"

    async def _sms(*_a: object, **_k: object) -> None:
        return None

    response_service._moderate_message = _allow  # type: ignore[assignment]
    response_service._moderate_text = _allow  # type: ignore[assignment]
    response_service._generate_reply = _reply  # type: ignore[assignment]
    response_service._send_sms = _sms  # type: ignore[assignment]

    async with session.begin():
        speaker = await get_or_create_speaker(session, user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, user_id)
        conversation = await get_or_create_conversation(session, speaker.id)
        user_utt = await create_utterance(session, conversation.id, speaker.id, "hello", meta=meta)
        bot_utt = await create_queued_utterance(
            session, conversation.id, bot.id, reply_to_id=user_utt.id
        )
        ids = (user_utt.id, bot_utt.id)

    await response_service._run_deferred_reply(user_id, ids[0], ids[1], _sessionmaker_from(session))


async def _issues(session: AsyncSession, user_id: str) -> list[PromptIssue]:
    session.expire_all()
    result = await session.execute(select(PromptIssue).where(PromptIssue.user_id == user_id))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_string_day_number_is_recorded_and_still_finds_its_prompt(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=7, content="DAY-7-CONTENT"))

    await _reply_with_metadata(async_session, "u-issue-str", {"day_number": "7"})

    issues = await _issues(async_session, "u-issue-str")
    assert [i.kind for i in issues] == [PROMPT_ISSUE_DAY_NUMBER_INVALID]
    assert "7" in issues[0].detail


@pytest.mark.asyncio
async def test_missing_daily_prompt_is_recorded(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _reply_with_metadata(async_session, "u-issue-nodaily", {"day_number": 31})

    issues = await _issues(async_session, "u-issue-nodaily")
    assert [i.kind for i in issues] == [PROMPT_ISSUE_DAILY_PROMPT_MISSING]
    assert "31" in issues[0].detail


@pytest.mark.asyncio
async def test_clean_request_records_nothing(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session.begin():
        async_session.add(DailyPrompt(day_number=3, content="DAY-3-CONTENT"))

    await _reply_with_metadata(async_session, "u-issue-clean", {"day_number": 3})

    assert await _issues(async_session, "u-issue-clean") == []


@pytest.mark.asyncio
async def test_absent_day_number_records_nothing(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A study may legitimately send turns with no day; that is not an error."""
    await _reply_with_metadata(async_session, "u-issue-noday", {})

    assert await _issues(async_session, "u-issue-noday") == []


@pytest.mark.asyncio
async def test_bad_day_number_records_only_the_format_issue(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable day_number cannot also be a 'missing prompt' — no day to look up."""
    await _reply_with_metadata(async_session, "u-issue-junk", {"day_number": "day seven"})

    issues = await _issues(async_session, "u-issue-junk")
    assert [i.kind for i in issues] == [PROMPT_ISSUE_DAY_NUMBER_INVALID]


@pytest.mark.asyncio
async def test_issue_links_back_to_the_utterance(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _reply_with_metadata(async_session, "u-issue-link", {"day_number": 31})

    issues = await _issues(async_session, "u-issue-link")
    assert issues[0].utterance_id is not None
