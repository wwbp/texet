from __future__ import annotations

from unittest.mock import patch

import pytest
from kani.models import ChatMessage, ChatRole  # type: ignore[import-untyped]

from app.engines.bedrock import (
    _FIRST_TURN_PLACEHOLDER,
    BedrockEngine,
    normalize_converse_messages,
)
from app.engines.factory import create_engine

# ---------------------------------------------------------------------------
# BedrockEngine unit tests
# ---------------------------------------------------------------------------


def _make_bedrock_response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


@pytest.fixture()
def bedrock_engine(monkeypatch: pytest.MonkeyPatch) -> BedrockEngine:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1")
    return engine


def test_bedrock_engine_context_size(bedrock_engine: BedrockEngine) -> None:
    assert bedrock_engine.max_context_size == 200_000


def test_llama_engine_context_size(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="meta.llama3-3-70b-instruct-v1:0")
    assert engine.max_context_size == 128_000


def test_llama_cross_region_engine_context_size(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="us.meta.llama3-3-70b-instruct-v1:0")
    assert engine.max_context_size == 128_000


@pytest.mark.asyncio
async def test_llama_predict_routes_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Llama on Bedrock Converse API: system messages routed to system= same as Claude."""
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="meta.llama3-3-70b-instruct-v1:0")

    captured: dict = {}

    def _fake_call(system_blocks: list, messages: list) -> dict:
        captured["system"] = system_blocks
        captured["messages"] = messages
        return _make_bedrock_response("Bonjour!")

    engine._call_bedrock = _fake_call  # type: ignore[method-assign]

    chat = [
        ChatMessage(role=ChatRole.SYSTEM, content="Reply in French."),
        ChatMessage(role=ChatRole.USER, content="Hello"),
    ]
    result = await engine.predict(chat)

    assert captured["system"] == [{"text": "Reply in French."}]
    assert captured["messages"] == [{"role": "user", "content": [{"text": "Hello"}]}]
    assert result.message.content == "Bonjour!"


def test_bedrock_engine_message_len(bedrock_engine: BedrockEngine) -> None:
    msg = ChatMessage(role=ChatRole.USER, content="hello world")
    assert bedrock_engine.message_len(msg) >= 1


@pytest.mark.asyncio
async def test_bedrock_predict_routes_system_prompt(bedrock_engine: BedrockEngine) -> None:
    """System messages must go to the system= parameter, not the messages list."""
    captured: dict = {}

    def _fake_call(system_blocks: list, messages: list) -> dict:
        captured["system"] = system_blocks
        captured["messages"] = messages
        return _make_bedrock_response("hello")

    bedrock_engine._call_bedrock = _fake_call  # type: ignore[method-assign]

    chat = [
        ChatMessage(role=ChatRole.SYSTEM, content="You are a helper."),
        ChatMessage(role=ChatRole.USER, content="Hi"),
    ]
    result = await bedrock_engine.predict(chat)

    assert captured["system"] == [{"text": "You are a helper."}]
    assert captured["messages"] == [{"role": "user", "content": [{"text": "Hi"}]}]
    assert result.message.content == "hello"
    assert result.message.role == ChatRole.ASSISTANT


@pytest.mark.asyncio
async def test_bedrock_predict_no_system_prompt(bedrock_engine: BedrockEngine) -> None:
    captured: dict = {}

    def _fake_call(system_blocks: list, messages: list) -> dict:
        captured["system"] = system_blocks
        captured["messages"] = messages
        return _make_bedrock_response("ok")

    bedrock_engine._call_bedrock = _fake_call  # type: ignore[method-assign]

    chat = [ChatMessage(role=ChatRole.USER, content="hello")]
    await bedrock_engine.predict(chat)

    assert captured["system"] == []
    assert len(captured["messages"]) == 1


# ---------------------------------------------------------------------------
# Converse message normalization tests
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def _capture_call(engine: BedrockEngine, captured: dict) -> None:
    def _fake_call(system_blocks: list, messages: list) -> dict:
        captured["system"] = system_blocks
        captured["messages"] = messages
        return _make_bedrock_response("ok")

    engine._call_bedrock = _fake_call  # type: ignore[method-assign]


def test_normalize_noop_for_alternating_history() -> None:
    conversation = [_user("a"), _assistant("b"), _user("c")]
    assert normalize_converse_messages(conversation) == conversation


def test_normalize_empty_input() -> None:
    assert normalize_converse_messages([]) == []


def test_normalize_merges_consecutive_user_messages() -> None:
    result = normalize_converse_messages([_user("a"), _user("b"), _assistant("c")])
    assert result == [_user("a\n\nb"), _assistant("c")]


def test_normalize_merges_consecutive_assistant_messages() -> None:
    result = normalize_converse_messages(
        [_user("hi"), _assistant("opening one"), _assistant("opening two"), _user("reply")]
    )
    assert result == [_user("hi"), _assistant("opening one\n\nopening two"), _user("reply")]


def test_normalize_prepends_placeholder_when_assistant_first() -> None:
    result = normalize_converse_messages([_assistant("hello"), _user("hi")])
    assert result == [_user(_FIRST_TURN_PLACEHOLDER), _assistant("hello"), _user("hi")]


def test_normalize_assistant_first_and_consecutive() -> None:
    """Merging happens before the placeholder is prepended, so the placeholder
    never merges into a following user message."""
    result = normalize_converse_messages([_assistant("a"), _assistant("b"), _user("c")])
    assert result == [_user(_FIRST_TURN_PLACEHOLDER), _assistant("a\n\nb"), _user("c")]


def test_normalize_drops_empty_messages() -> None:
    result = normalize_converse_messages([_user(""), _assistant("   "), _user("hello")])
    assert result == [_user("hello")]


@pytest.mark.asyncio
async def test_bedrock_predict_normalizes_assistant_first(bedrock_engine: BedrockEngine) -> None:
    """Assistant-first history (e.g. a hub opening message) gets a placeholder
    user turn; the system prompt still routes to system=."""
    captured: dict = {}
    _capture_call(bedrock_engine, captured)

    chat = [
        ChatMessage(role=ChatRole.SYSTEM, content="You are a helper."),
        ChatMessage(role=ChatRole.ASSISTANT, content="Welcome!"),
        ChatMessage(role=ChatRole.USER, content="Hi"),
    ]
    await bedrock_engine.predict(chat)

    assert captured["system"] == [{"text": "You are a helper."}]
    assert captured["messages"] == [
        _user(_FIRST_TURN_PLACEHOLDER),
        _assistant("Welcome!"),
        _user("Hi"),
    ]


@pytest.mark.asyncio
async def test_bedrock_predict_drops_none_content(bedrock_engine: BedrockEngine) -> None:
    captured: dict = {}
    _capture_call(bedrock_engine, captured)

    chat = [
        ChatMessage(role=ChatRole.ASSISTANT, content=None),
        ChatMessage(role=ChatRole.USER, content="hello"),
    ]
    await bedrock_engine.predict(chat)

    assert captured["messages"] == [_user("hello")]


# ---------------------------------------------------------------------------
# Engine factory tests
# ---------------------------------------------------------------------------


def test_factory_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    from kani.engines.openai import OpenAIEngine  # type: ignore[import-untyped]

    with patch.object(OpenAIEngine, "__init__", return_value=None):
        engine = create_engine("openai", "gpt-4o-mini")
    assert isinstance(engine, OpenAIEngine)


def test_factory_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    with patch("boto3.client"):
        engine = create_engine("bedrock", "us.anthropic.claude-sonnet-4-6")
    assert isinstance(engine, BedrockEngine)


def test_factory_openai_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_engine("openai", "gpt-4o-mini")


def test_factory_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported provider"):
        create_engine("anthropic", "claude-3")
