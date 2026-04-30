from __future__ import annotations

import asyncio

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from kani.engines.base import BaseCompletion, BaseEngine  # type: ignore[import-untyped]
from kani.models import ChatMessage, ChatRole  # type: ignore[import-untyped]


class BedrockCompletion(BaseCompletion):
    def __init__(self, message: ChatMessage) -> None:
        self._message = message

    @property
    def message(self) -> ChatMessage:
        return self._message

    @property
    def prompt_tokens(self) -> int | None:
        return None

    @property
    def completion_tokens(self) -> int | None:
        return None


_CONTEXT_SIZES: dict[str, int] = {
    # Anthropic Claude
    "us.anthropic.claude-sonnet-4-6": 200_000,
    "us.anthropic.claude-sonnet-4-5": 200_000,
    "anthropic.claude-3-5-sonnet-20241022-v2:0": 200_000,
    "anthropic.claude-3-5-sonnet-20240620-v1:0": 200_000,
    "anthropic.claude-3-sonnet-20240229-v1:0": 200_000,
    "anthropic.claude-3-haiku-20240307-v1:0": 200_000,
    # Meta Llama
    "meta.llama3-3-70b-instruct-v1:0": 128_000,
    "us.meta.llama3-3-70b-instruct-v1:0": 128_000,
}


class BedrockEngine(BaseEngine):
    """Kani engine backed by Amazon Bedrock Converse API."""

    def __init__(
        self,
        model_id: str,
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        region_name: str = "us-east-1",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_context_size = _CONTEXT_SIZES.get(model_id, 8192)

        client_kwargs: dict[str, object] = {"region_name": region_name}
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        self.client = boto3.client("bedrock-runtime", **client_kwargs)

    def message_len(self, message: ChatMessage) -> int:
        content = message.content if isinstance(message.content, str) else str(message.content)
        return max(1, len(content) // 4)

    async def prompt_len(
        self,
        messages: list[ChatMessage],
        functions: list | None = None,
        **kwargs: object,
    ) -> int:
        return sum(self.message_len(m) for m in messages)

    async def predict(
        self,
        messages: list[ChatMessage],
        functions: list | None = None,
        **kwargs: object,
    ) -> BedrockCompletion:
        system_blocks: list[dict] = []
        conversation: list[dict] = []

        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if msg.role == ChatRole.SYSTEM:
                system_blocks.append({"text": content})
            else:
                role = "user" if msg.role == ChatRole.USER else "assistant"
                conversation.append({"role": role, "content": [{"text": content}]})

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, self._call_bedrock, system_blocks, conversation)
        text = response["output"]["message"]["content"][0]["text"].strip()
        return BedrockCompletion(ChatMessage(role=ChatRole.ASSISTANT, content=text))

    def _call_bedrock(self, system_blocks: list[dict], messages: list[dict]) -> dict:
        converse_kwargs: dict[str, object] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        if system_blocks:
            converse_kwargs["system"] = system_blocks
        try:
            return self.client.converse(**converse_kwargs)  # type: ignore[no-any-return]
        except ClientError as exc:
            raise RuntimeError(f"Bedrock API call failed: {exc}") from exc

    async def close(self) -> None:
        pass
