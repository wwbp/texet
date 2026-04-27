from typing import Any, Literal

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


ResponseMode = Literal["text"]


class ResponseRequest(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "user_id": "u1",
                    "input": "hello",
                    "mode": "text",
                    "metadata": {"source": "sms", "day_identifier": 1},
                }
            ]
        },
    )

    user_id: str = Field(min_length=1, max_length=128)
    input: str
    mode: ResponseMode = "text"
    metadata: dict[str, Any] | None = None


class ResponseQueuedResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "id": "reply_utterance_id",
                    "object": "response",
                    "status": "queued",
                    "conversation_id": "conversation_id",
                    "mode": "text",
                }
            ]
        },
    )
    id: str
    object: Literal["response"]
    status: Literal["queued"]
    conversation_id: str
    mode: ResponseMode
