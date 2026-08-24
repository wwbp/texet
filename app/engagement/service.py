from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.response import Conversation, Utterance
from app.response.crud import bot_speaker_id
from app.response.utils import extract_utc_offset

# The hub marks the message it sends to open a participant's day. That mark is
# what makes a day a "pinged" day; without one there was nothing to respond to,
# so the day is not a missed day and does not appear in the report.
HUB_INITIAL_META_KEY = "texet_hub_initial"
USAGE_META_KEY = "texet_usage"


@dataclass(frozen=True)
class EngagementDay:
    """One participant, one calendar day on which the chatbot pinged them."""

    participant_id: str
    date: datetime.date
    engaged: bool
    utterance_count: int
    token_count: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "date": self.date.isoformat(),
            "engaged": self.engaged,
            "utterance_count": self.utterance_count,
            "token_count": self.token_count,
        }


def _is_ping(utterance: Utterance) -> bool:
    return bool((utterance.meta or {}).get(HUB_INITIAL_META_KEY))


def _usage_total(utterance: Utterance) -> int | None:
    usage = (utterance.meta or {}).get(USAGE_META_KEY)
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return prompt + completion


@dataclass
class _DayTally:
    pinged: bool = False
    participant_messages: int = 0
    tokens: int | None = None

    def add_tokens(self, amount: int | None) -> None:
        # None means the provider reported nothing, which is not zero. A day is
        # only unknown if nothing in it was measured.
        if amount is None:
            return
        self.tokens = amount if self.tokens is None else self.tokens + amount


async def compute_engagement(
    session: AsyncSession,
    *,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
) -> list[EngagementDay]:
    """Engagement per participant per pinged calendar day.

    A day counts as engaged when the participant sent at least one message on
    the calendar day the chatbot pinged them. Days are the participant's local
    days, taken from the same user_local_time metadata the day markers use, so
    a late-evening reply is not counted against the following day. Participants
    with no recorded offset fall back to UTC.
    """
    conditions = []
    if start is not None:
        # A day of padding on each side: a participant's local day can begin
        # before, and end after, the same-numbered UTC day.
        conditions.append(Utterance.timestamp >= _as_utc_start(start - datetime.timedelta(days=1)))
    if end is not None:
        conditions.append(Utterance.timestamp < _as_utc_start(end + datetime.timedelta(days=2)))

    result = await session.execute(
        select(Utterance, Conversation.owner_speaker_id)
        .join(Conversation, Utterance.conversation_id == Conversation.id)
        .where(*conditions)
        .order_by(Utterance.timestamp)
    )
    rows = result.all()

    # One offset per participant, from the first utterance that records one, so
    # that a participant's days stay on a single clock even though only some
    # messages carry the metadata.
    offsets: dict[str, datetime.timedelta] = {}
    for utterance, owner in rows:
        if owner not in offsets:
            offset = extract_utc_offset(utterance.meta)
            if offset is not None:
                offsets[owner] = offset

    tallies: dict[tuple[str, datetime.date], _DayTally] = {}
    for utterance, owner in rows:
        tz = datetime.timezone(offsets[owner]) if owner in offsets else datetime.UTC
        day = utterance.timestamp.astimezone(tz).date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue

        tally = tallies.setdefault((owner, day), _DayTally())
        if utterance.speaker_id == owner:
            if utterance.text:
                tally.participant_messages += 1
        elif utterance.speaker_id == bot_speaker_id(owner):
            if _is_ping(utterance):
                tally.pinged = True
            tally.add_tokens(_usage_total(utterance))

    return [
        EngagementDay(
            participant_id=participant,
            date=day,
            engaged=tally.participant_messages > 0,
            utterance_count=tally.participant_messages,
            token_count=tally.tokens,
        )
        for (participant, day), tally in sorted(tallies.items())
        if tally.pinged
    ]


def _as_utc_start(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
