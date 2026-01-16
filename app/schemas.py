from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import MESSAGE_MAX_LENGTH, MESSAGE_MIN_LENGTH


class MessagePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(
        min_length=MESSAGE_MIN_LENGTH,
        max_length=MESSAGE_MAX_LENGTH,
    )


class ChatRequest(MessagePayload):
    pass


class SmsOutboundRequest(MessagePayload):
    pass


class ChatQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    reply_utterance_id: str
    status: Literal["queued"]
