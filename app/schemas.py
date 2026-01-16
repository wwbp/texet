from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MessagePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class ChatRequest(MessagePayload):
    pass


class SmsOutboundRequest(MessagePayload):
    pass


class ChatQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    reply_utterance_id: str
    status: Literal["queued"]
