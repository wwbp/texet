"""Pure-function tests for the v2 ConvoKit export: no database."""

from __future__ import annotations

import datetime
import hashlib

from app.console.exports import infer_provenance, slim_generation
from app.models.response import DailyPrompt
from app.response.prompt import compose_instruction_prompt
from app.response.service import _provenance_meta
from app.response.utils import prompt_sha256

TZ = datetime.UTC
REPLY_AT = datetime.datetime(2026, 3, 10, 12, 0, tzinfo=TZ)
BEFORE = REPLY_AT - datetime.timedelta(days=1)
AFTER = REPLY_AT + datetime.timedelta(days=1)


def _snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "version": 2,
        "provider": "bedrock",
        "model_id": "llama",
        "system_prompt": "SYSTEM",
        "chat_history": [{"role": "user", "content": "old"}],
        "query": "hi",
        "week_start": "2026-03-08",
        "day_number": 3,
        "user_local_time": None,
    }
    snapshot.update(overrides)
    return snapshot


def test_prompt_sha256_is_hex_sha256_of_utf8() -> None:
    assert prompt_sha256("é") == hashlib.sha256("é".encode()).hexdigest()


def test_provenance_meta_flat_fields() -> None:
    meta = _provenance_meta(
        provider="bedrock",
        model_id="llama",
        system_prompt_id="sp1",
        instruction_template_id=None,
        daily_prompt_id=None,
        daily_prompt_missing=True,
        weekly_summary_id="ws1",
        week_start=datetime.date(2026, 3, 8),
        day_number=3,
        system_prompt="SYSTEM",
    )
    assert meta == {
        "texet_provider": "bedrock",
        "texet_model_id": "llama",
        "texet_system_prompt_id": "sp1",
        "texet_instruction_template_id": None,
        "texet_daily_prompt_id": None,
        "texet_daily_prompt_missing": True,
        "texet_weekly_summary_id": "ws1",
        "texet_week_start": "2026-03-08",
        "texet_day_number": 3,
        "texet_system_prompt_sha256": prompt_sha256("SYSTEM"),
    }


def test_provenance_meta_omits_missing_flag_when_prompt_found() -> None:
    meta = _provenance_meta(
        provider="bedrock",
        model_id="llama",
        system_prompt_id=None,
        instruction_template_id=None,
        daily_prompt_id="dp1",
        daily_prompt_missing=False,
        weekly_summary_id=None,
        week_start=datetime.date(2026, 3, 8),
        day_number=None,
        system_prompt="S",
    )
    assert "texet_daily_prompt_missing" not in meta


def test_slim_generation_drops_bulk_and_adds_hash() -> None:
    meta = {"texet_generation": _snapshot(), "other": 1}
    slim = slim_generation(meta, include_generation_snapshot=False)
    assert "system_prompt" not in slim["texet_generation"]
    assert "chat_history" not in slim["texet_generation"]
    assert slim["texet_generation"]["query"] == "hi"
    assert slim["texet_system_prompt_sha256"] == prompt_sha256("SYSTEM")
    assert slim["other"] == 1
    # Input is not mutated.
    assert "chat_history" in meta["texet_generation"]  # type: ignore[operator]


def test_slim_generation_keeps_snapshot_when_asked() -> None:
    slim = slim_generation({"texet_generation": _snapshot()}, include_generation_snapshot=True)
    assert slim["texet_generation"]["chat_history"] == [{"role": "user", "content": "old"}]
    assert slim["texet_system_prompt_sha256"] == prompt_sha256("SYSTEM")


def test_slim_generation_keeps_a_stored_hash() -> None:
    slim = slim_generation(
        {"texet_generation": _snapshot(), "texet_system_prompt_sha256": "stored"},
        include_generation_snapshot=False,
    )
    assert slim["texet_system_prompt_sha256"] == "stored"


def test_slim_generation_without_snapshot_is_identity() -> None:
    assert slim_generation({"a": 1}, include_generation_snapshot=False) == {"a": 1}


def _infer(
    meta: dict[str, object],
    daily: dict[int, DailyPrompt] | None = None,
    summaries: dict[tuple[str, datetime.date], tuple[str, datetime.datetime]] | None = None,
    missing: set[str] | None = None,
) -> dict[str, object]:
    return infer_provenance(
        meta,
        timestamp=REPLY_AT,
        owner_id="u1",
        reply_to_id="user-utt",
        daily_prompts=daily or {},
        summaries=summaries or {},
        daily_missing_for=missing or set(),
    )


def test_infer_skips_rows_with_provenance() -> None:
    meta = {"texet_generation": _snapshot(), "texet_model_id": "llama"}
    assert _infer(meta) == {}


def test_infer_skips_rows_without_snapshot() -> None:
    assert _infer({"source": "sms"}) == {}


def test_infer_old_reply() -> None:
    daily = {3: DailyPrompt(id="dp3", day_number=3, content="c", created_at=BEFORE)}
    # The prompt injects the PREVIOUS week's summary.
    summaries = {("u1", datetime.date(2026, 3, 1)): ("ws-prev", BEFORE)}
    assert _infer({"texet_generation": _snapshot()}, daily, summaries) == {
        "texet_provider": "bedrock",
        "texet_model_id": "llama",
        "texet_system_prompt_id": None,
        "texet_instruction_template_id": None,
        "texet_daily_prompt_id": "dp3",
        "texet_weekly_summary_id": "ws-prev",
        "texet_week_start": "2026-03-08",
        "texet_day_number": 3,
        "texet_config_inferred": True,
    }


def test_infer_ignores_config_created_after_reply() -> None:
    daily = {3: DailyPrompt(id="dp3", day_number=3, content="c", created_at=AFTER)}
    summaries = {("u1", datetime.date(2026, 3, 1)): ("ws-prev", AFTER)}
    inferred = _infer({"texet_generation": _snapshot()}, daily, summaries)
    assert inferred["texet_daily_prompt_id"] is None
    assert inferred["texet_weekly_summary_id"] is None


def test_infer_marks_daily_prompt_missing_from_prompt_issue() -> None:
    inferred = _infer({"texet_generation": _snapshot()}, missing={"user-utt"})
    assert inferred["texet_daily_prompt_missing"] is True
    assert inferred["texet_daily_prompt_id"] is None


def test_composed_prompt_hash_differs_per_participant_under_one_config() -> None:
    """Same config, different weekly summary: so the hash is not a config version."""

    def composed(summary: str) -> str:
        return compose_instruction_prompt(
            base="BASE",
            daily_content="DAILY",
            weekly_summary=summary,
            user_local_time=None,
            day_number=3,
            template="{base}\n\n{daily_content}\n\n{weekly_summary}",
        )

    assert prompt_sha256(composed("alice")) != prompt_sha256(composed("bob"))
