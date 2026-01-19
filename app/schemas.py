from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    message: str


class ChatQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    reply_utterance_id: str
    status: Literal["queued"]
