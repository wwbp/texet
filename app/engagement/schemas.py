from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict


class EngagementRow(BaseModel):
    """One participant, one calendar day the chatbot pinged them."""

    model_config = ConfigDict(extra="forbid")

    participant_id: str
    date: datetime.date
    engaged: bool
    utterance_count: int
    # None means no reply that day carried provider-reported usage — every
    # utterance generated before usage capture looks like this. Zero would
    # claim the day was free, which is a different and wrong statement.
    token_count: int | None
