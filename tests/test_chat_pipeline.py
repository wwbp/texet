import pytest

from app.constants import MESSAGE_MAX_LENGTH
from app.services import chat as chat_service


@pytest.mark.asyncio
async def test_pipeline_echo() -> None:
    result = await chat_service._run_pipeline("hello")
    assert result == "echo:hello"


@pytest.mark.asyncio
async def test_pipeline_rejects_empty_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _generate_reply(_: str) -> str:
        return ""

    monkeypatch.setattr(chat_service, "_generate_reply", _generate_reply)

    with pytest.raises(RuntimeError, match="pipeline:qa failed"):
        await chat_service._run_pipeline("hello")


@pytest.mark.asyncio
async def test_pipeline_rejects_long_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _generate_reply(_: str) -> str:
        return "a" * (MESSAGE_MAX_LENGTH + 1)

    monkeypatch.setattr(chat_service, "_generate_reply", _generate_reply)

    with pytest.raises(RuntimeError, match="pipeline:qa failed"):
        await chat_service._run_pipeline("hello")
