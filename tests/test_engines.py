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


# ---------------------------------------------------------------------------
# Llama 4: the models prod actually runs on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "meta.llama4-maverick-17b-instruct-v1:0",
        "us.meta.llama4-maverick-17b-instruct-v1:0",
        "meta.llama4-scout-17b-instruct-v1:0",
        "us.meta.llama4-scout-17b-instruct-v1:0",
    ],
)
def test_llama4_engines_declare_a_context_size(model_id: str) -> None:
    """Prod replies run on us.meta.llama4-maverick-17b-instruct-v1:0, which was
    missing from the table and so silently got the 8192 fallback — 15x under a
    llama3-3 sibling. Weekly summaries are the largest single-message workload
    in the system, so that fallback is what would clip them first."""
    with patch("boto3.client"):
        engine = BedrockEngine(model_id=model_id)
    assert engine.max_context_size >= 128_000


def test_unknown_model_still_falls_back() -> None:
    """The fallback stays for genuinely unknown ids; only known ones are named."""
    with patch("boto3.client"):
        engine = BedrockEngine(model_id="meta.llama-does-not-exist-v9:0")
    assert engine.max_context_size == 8192


def test_the_pinned_summary_model_has_a_declared_context_size() -> None:
    """Couples the pin to the table: pinning summaries to a model whose context
    size is unknown would hand the largest prompt in the system the 8192
    fallback, which is the failure this guards against."""
    from app.engines.bedrock import _CONTEXT_SIZES
    from app.summary.service import SUMMARY_MODEL_ID, SUMMARY_PROVIDER

    assert SUMMARY_PROVIDER == "bedrock"
    assert SUMMARY_MODEL_ID in _CONTEXT_SIZES


# ---------------------------------------------------------------------------
# Token usage: Bedrock returns it, the engine used to drop it
# ---------------------------------------------------------------------------


def _bedrock_response_with_usage(text: str, inp: int, out: int) -> dict:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out},
    }


@pytest.mark.asyncio
async def test_predict_surfaces_token_usage(bedrock_engine: BedrockEngine) -> None:
    """Converse reports usage on every call; BedrockCompletion returned None for
    both counts, so the only per-reply cost signal the app had was thrown away."""
    bedrock_engine._call_bedrock = lambda *_a: _bedrock_response_with_usage("hi", 120, 45)  # type: ignore[method-assign]

    completion = await bedrock_engine.predict([ChatMessage(role=ChatRole.USER, content="hello")])

    assert completion.prompt_tokens == 120
    assert completion.completion_tokens == 45
    assert bedrock_engine.last_usage == {"prompt_tokens": 120, "completion_tokens": 45}


@pytest.mark.asyncio
async def test_predict_without_a_usage_block_reports_nothing(
    bedrock_engine: BedrockEngine,
) -> None:
    """A response with no usage must not invent zeros — zero tokens and unknown
    tokens are different facts, and the endpoint reports them differently."""
    bedrock_engine._call_bedrock = lambda *_a: _make_bedrock_response("hi")  # type: ignore[method-assign]

    completion = await bedrock_engine.predict([ChatMessage(role=ChatRole.USER, content="hello")])

    assert completion.prompt_tokens is None
    assert completion.completion_tokens is None
    assert bedrock_engine.last_usage is None


@pytest.mark.asyncio
async def test_last_usage_reflects_the_most_recent_call(bedrock_engine: BedrockEngine) -> None:
    """The engine is per-generation today, but a reused one must not report a
    stale count for a call that returned none."""
    bedrock_engine._call_bedrock = lambda *_a: _bedrock_response_with_usage("one", 10, 5)  # type: ignore[method-assign]
    await bedrock_engine.predict([ChatMessage(role=ChatRole.USER, content="a")])
    assert bedrock_engine.last_usage == {"prompt_tokens": 10, "completion_tokens": 5}

    bedrock_engine._call_bedrock = lambda *_a: _make_bedrock_response("two")  # type: ignore[method-assign]
    await bedrock_engine.predict([ChatMessage(role=ChatRole.USER, content="b")])
    assert bedrock_engine.last_usage is None
