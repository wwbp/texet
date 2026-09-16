"""Console-managed moderation settings: the email lever and the per-category
thresholds, plus the service behaviour they drive."""

import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MODERATION_VALUES_FOR_BLOCKED
from app.db import get_engine
from app.main import app
from app.models.admin import ModerationSettings
from app.response import service as response_service
from app.response.crud import get_moderation_settings


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def console_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-admin-secret")
    database_url_test = os.getenv("DATABASE_URL_TEST")
    if not database_url_test:
        pytest.skip("DATABASE_URL_TEST is not set.")
    monkeypatch.setenv("DATABASE_URL", database_url_test)
    get_engine.cache_clear()


@pytest.fixture()
async def console_client(console_env: None) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Resolution: no row means built-in defaults, with email off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defaults_when_no_row(async_session: AsyncSession) -> None:
    email_enabled, thresholds = await get_moderation_settings(async_session)

    assert email_enabled is False
    assert thresholds == MODERATION_VALUES_FOR_BLOCKED


@pytest.mark.asyncio
async def test_stored_thresholds_override_defaults(async_session: AsyncSession) -> None:
    async with async_session.begin():
        async_session.add(
            ModerationSettings(id=1, email_enabled=True, thresholds={"self-harm": 0.9})
        )

    email_enabled, thresholds = await get_moderation_settings(async_session)

    assert email_enabled is True
    assert thresholds["self-harm"] == 0.9
    # Categories with no stored override keep their built-in value.
    assert thresholds["sexual/minors"] == MODERATION_VALUES_FOR_BLOCKED["sexual/minors"]


# ---------------------------------------------------------------------------
# The email lever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moderation_email_not_sent_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERATION_ALERT_EMAILS", "alerts@example.com")
    monkeypatch.setenv("MAIL_USERNAME", "u")
    monkeypatch.setenv("MAIL_PASSWORD", "p")
    monkeypatch.setenv("MAIL_FROM", "bot@example.com")
    monkeypatch.setenv("MAIL_SERVER", "smtp.example.com")

    sent: list[object] = []
    monkeypatch.setattr(
        response_service,
        "_build_moderation_email",
        lambda **kwargs: sent.append(kwargs) or ("subject", "body"),
    )

    await response_service._send_moderation_email(
        user_id="u-1",
        utterance_id="utt-1",
        conversation_id="conv-1",
        speaker_id="spk-1",
        utterance_text="text",
        utterance_timestamp=None,
        blocked_category="self-harm",
        blocked_score=0.9,
        recent_chat_history=[],
        email_enabled=False,
    )

    assert sent == []


class _ReachedTheSend(Exception):
    """Raised from the stubbed builder to prove the lever let the call through."""


@pytest.mark.asyncio
async def test_moderation_email_sent_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERATION_ALERT_EMAILS", "alerts@example.com")
    monkeypatch.setenv("MAIL_USERNAME", "u")
    monkeypatch.setenv("MAIL_PASSWORD", "p")
    monkeypatch.setenv("MAIL_FROM", "bot@example.com")
    monkeypatch.setenv("MAIL_SERVER", "smtp.example.com")

    def _explode(**_kwargs: object) -> tuple[str, str]:
        raise _ReachedTheSend

    monkeypatch.setattr(response_service, "_build_moderation_email", _explode)

    with pytest.raises(_ReachedTheSend):
        await response_service._send_moderation_email(
            user_id="u-1",
            utterance_id="utt-1",
            conversation_id="conv-1",
            speaker_id="spk-1",
            utterance_text="text",
            utterance_timestamp=None,
            blocked_category="self-harm",
            blocked_score=0.9,
            recent_chat_history=[],
            email_enabled=True,
        )


# ---------------------------------------------------------------------------
# Thresholds drive _moderate_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stored_threshold_blocks_below_builtin_default(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A console-lowered threshold blocks a score the built-in default allows."""
    async with async_session.begin():
        async_session.add(
            ModerationSettings(id=1, email_enabled=False, thresholds={"violence": 0.1})
        )
    _, thresholds = await get_moderation_settings(async_session)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(response_service, "mock_external_apis", lambda: False)
    _stub_moderation_openai(monkeypatch, {"violence": 0.5})

    blocked, _, category, score = await response_service._moderate_text("sample input", thresholds)

    assert blocked is True
    assert category == "violence"
    assert score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_stored_threshold_can_disable_a_builtin_category(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with async_session.begin():
        async_session.add(
            ModerationSettings(id=1, email_enabled=False, thresholds={"self-harm": 1.0})
        )
    _, thresholds = await get_moderation_settings(async_session)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(response_service, "mock_external_apis", lambda: False)
    _stub_moderation_openai(monkeypatch, {"self-harm": 0.99})

    blocked, _, _, _ = await response_service._moderate_text("sample input", thresholds)

    assert blocked is False


def _stub_moderation_openai(
    monkeypatch: pytest.MonkeyPatch, category_scores: dict[str, float]
) -> None:
    from types import SimpleNamespace

    response = SimpleNamespace(results=[SimpleNamespace(category_scores=category_scores)])

    class _FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            self.moderations = SimpleNamespace(create=self._create)

        async def _create(self, *, input: str, model: str) -> SimpleNamespace:
            return response

        async def close(self) -> None:
            return None

    monkeypatch.setattr(response_service, "AsyncOpenAI", _FakeOpenAI)


# ---------------------------------------------------------------------------
# Console page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_console_moderation_requires_auth(console_client: AsyncClient) -> None:
    assert (await console_client.get("/console/moderation")).status_code == 401
    assert (await console_client.post("/console/moderation", data={})).status_code == 401


@pytest.mark.asyncio
async def test_console_moderation_renders_defaults(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    response = await console_client.get(
        "/console/moderation", headers=_basic_auth_header("admin", "secret")
    )

    assert response.status_code == 200
    # Every category the study knows about is listed and editable.
    for category in MODERATION_VALUES_FOR_BLOCKED:
        assert f'name="threshold:{category}"' in response.text
    assert 'name="email_enabled"' in response.text


@pytest.mark.asyncio
async def test_console_moderation_saves_lever_and_thresholds(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    headers = _basic_auth_header("admin", "secret")
    form = {
        f"threshold:{name}": str(value) for name, value in MODERATION_VALUES_FOR_BLOCKED.items()
    }
    form["threshold:self-harm"] = "0.35"
    form["email_enabled"] = "on"

    response = await console_client.post("/console/moderation", data=form, headers=headers)
    assert response.status_code == 200

    email_enabled, thresholds = await get_moderation_settings(async_session)
    assert email_enabled is True
    assert thresholds["self-harm"] == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_console_moderation_unchecked_box_turns_email_off(
    console_client: AsyncClient, async_session: AsyncSession
) -> None:
    headers = _basic_auth_header("admin", "secret")
    form = {
        f"threshold:{name}": str(value) for name, value in MODERATION_VALUES_FOR_BLOCKED.items()
    }

    await console_client.post(
        "/console/moderation", data={**form, "email_enabled": "on"}, headers=headers
    )
    # An unchecked checkbox is simply absent from the POST body.
    await console_client.post("/console/moderation", data=form, headers=headers)

    email_enabled, _ = await get_moderation_settings(async_session)
    assert email_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["-0.1", "1.5", "abc", ""])
async def test_console_moderation_rejects_out_of_range_threshold(
    console_client: AsyncClient, async_session: AsyncSession, bad: str
) -> None:
    headers = _basic_auth_header("admin", "secret")
    form = {
        f"threshold:{name}": str(value) for name, value in MODERATION_VALUES_FOR_BLOCKED.items()
    }
    form["threshold:self-harm"] = bad

    response = await console_client.post("/console/moderation", data=form, headers=headers)

    assert response.status_code == 400
    # Nothing was persisted.
    _, thresholds = await get_moderation_settings(async_session)
    assert thresholds["self-harm"] == MODERATION_VALUES_FOR_BLOCKED["self-harm"]
