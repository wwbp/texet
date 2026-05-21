"""Unit tests for _build_moderation_email — no DB, no mail server needed."""
import datetime

from kani import ChatMessage, ChatRole

from app.response.service import _build_moderation_email

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "https://texet.example.com"
_EST = datetime.timezone(datetime.timedelta(hours=-5), name="EST")
_DEFAULT_TIMESTAMP = datetime.datetime(2026, 5, 14, 15, 42, tzinfo=_EST)


def _build(
    *,
    user_id: str = "u-123",
    utterance_id: str = "utt-abc",
    conversation_id: str = "conv-xyz",
    speaker_id: str = "spk-456",
    utterance_text: str = "I want to hurt myself",
    utterance_timestamp: datetime.datetime = _DEFAULT_TIMESTAMP,
    blocked_category: str = "self-harm/intent",
    blocked_score: float = 0.82,
    history: list[ChatMessage] | None = None,
    admin_base_url: str = _BASE_URL,
) -> tuple[str, str]:
    return _build_moderation_email(
        user_id=user_id,
        utterance_id=utterance_id,
        conversation_id=conversation_id,
        speaker_id=speaker_id,
        utterance_text=utterance_text,
        utterance_timestamp=utterance_timestamp,
        blocked_category=blocked_category,
        blocked_score=blocked_score,
        recent_chat_history=history or [],
        admin_base_url=admin_base_url,
    )


# ---------------------------------------------------------------------------
# Subject line
# ---------------------------------------------------------------------------


def test_subject_contains_category() -> None:
    subject, _ = _build(blocked_category="self-harm/intent")
    assert "self-harm/intent" in subject


def test_subject_contains_score_as_percentage() -> None:
    subject, _ = _build(blocked_score=0.82)
    assert "82%" in subject


def test_subject_contains_user_id() -> None:
    subject, _ = _build(user_id="u-999")
    assert "u-999" in subject


def test_subject_has_texet_prefix() -> None:
    subject, _ = _build()
    assert subject.startswith("[texet]")


# ---------------------------------------------------------------------------
# Severity colour coding
# ---------------------------------------------------------------------------


def test_high_score_uses_red() -> None:
    _, body = _build(blocked_score=0.75)
    assert "#c0392b" in body


def test_medium_score_uses_orange() -> None:
    _, body = _build(blocked_score=0.50)
    assert "#d35400" in body


def test_low_score_uses_yellow() -> None:
    _, body = _build(blocked_score=0.30)
    assert "#f39c12" in body


def test_score_boundary_07_is_red() -> None:
    _, body = _build(blocked_score=0.70)
    assert "#c0392b" in body


def test_score_boundary_04_is_orange() -> None:
    _, body = _build(blocked_score=0.40)
    assert "#d35400" in body


# ---------------------------------------------------------------------------
# Flagged message appears prominently and early
# ---------------------------------------------------------------------------


def test_flagged_message_in_body() -> None:
    _, body = _build(utterance_text="I want to hurt myself")
    assert "I want to hurt myself" in body


def test_flagged_message_appears_before_admin_links() -> None:
    _, body = _build(utterance_text="the flagged text")
    assert body.index("the flagged text") < body.index("Admin links")


def test_flagged_message_appears_before_recent_context() -> None:
    history = [ChatMessage(role=ChatRole.USER, content="context message")]
    _, body = _build(utterance_text="the flagged text", history=history)
    assert body.index("the flagged text") < body.index("context message")


def test_flagged_message_section_header_present() -> None:
    _, body = _build()
    assert "Flagged message" in body


# ---------------------------------------------------------------------------
# Meta fields (user, category, score)
# ---------------------------------------------------------------------------


def test_user_id_in_body() -> None:
    _, body = _build(user_id="u-123")
    assert "u-123" in body


def test_category_in_body() -> None:
    _, body = _build(blocked_category="harassment/threatening")
    assert "harassment/threatening" in body


def test_score_formatted_to_two_decimal_places() -> None:
    _, body = _build(blocked_score=0.82)
    assert "0.82" in body


# ---------------------------------------------------------------------------
# Admin links
# ---------------------------------------------------------------------------


def test_admin_links_section_present_when_url_configured() -> None:
    _, body = _build(admin_base_url=_BASE_URL)
    assert "Admin links" in body


def test_utterance_link_correct() -> None:
    _, body = _build(utterance_id="utt-abc", admin_base_url=_BASE_URL)
    assert f"{_BASE_URL}/console/admin/utterance/details/utt-abc" in body


def test_conversation_link_correct() -> None:
    _, body = _build(conversation_id="conv-xyz", admin_base_url=_BASE_URL)
    assert f"{_BASE_URL}/console/admin/conversation/details/conv-xyz" in body


def test_speaker_link_correct() -> None:
    _, body = _build(speaker_id="spk-456", admin_base_url=_BASE_URL)
    assert f"{_BASE_URL}/console/admin/speaker/details/spk-456" in body


def test_trailing_slash_on_base_url_does_not_double_slash() -> None:
    _, body = _build(admin_base_url="https://texet.example.com/", utterance_id="utt-abc")
    assert "//console" not in body
    assert "https://texet.example.com/console/admin/utterance/details/utt-abc" in body


def test_no_admin_links_when_url_not_configured() -> None:
    _, body = _build(admin_base_url="")
    assert "Admin links" not in body
    assert "/console/admin/" not in body


# ---------------------------------------------------------------------------
# Recent context / chat history
# ---------------------------------------------------------------------------


def test_history_messages_appear_in_body() -> None:
    history = [
        ChatMessage(role=ChatRole.USER, content="how are you"),
        ChatMessage(role=ChatRole.ASSISTANT, content="I'm good"),
    ]
    _, body = _build(history=history)
    assert "how are you" in body
    assert "I'm good" in body


def test_history_section_header_present_when_history_given() -> None:
    history = [ChatMessage(role=ChatRole.USER, content="hello")]
    _, body = _build(history=history)
    assert "Recent context" in body


def test_no_history_section_when_history_empty() -> None:
    _, body = _build(history=[])
    assert "Recent context" not in body


def test_history_role_labels_present() -> None:
    history = [
        ChatMessage(role=ChatRole.USER, content="msg1"),
        ChatMessage(role=ChatRole.ASSISTANT, content="msg2"),
    ]
    _, body = _build(history=history)
    assert "user" in body
    assert "assistant" in body


def test_history_appears_after_admin_links() -> None:
    history = [ChatMessage(role=ChatRole.USER, content="context text")]
    _, body = _build(history=history)
    assert body.index("Admin links") < body.index("Recent context")


# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


def test_utterance_text_html_escaped() -> None:
    _, body = _build(utterance_text="<script>alert('xss')</script>")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_user_id_html_escaped() -> None:
    _, body = _build(user_id="u<1>&2")
    assert "u<1>&2" not in body
    assert "u&lt;1&gt;&amp;2" in body


def test_category_html_escaped() -> None:
    _, body = _build(blocked_category="cat<&>")
    assert "cat<&>" not in body
    assert "cat&lt;&amp;&gt;" in body


def test_history_text_html_escaped() -> None:
    history = [ChatMessage(role=ChatRole.USER, content="<b>bad</b>")]
    _, body = _build(history=history)
    assert "<b>bad</b>" not in body
    assert "&lt;b&gt;bad&lt;/b&gt;" in body


# ---------------------------------------------------------------------------
# HTML structure
# ---------------------------------------------------------------------------


def test_body_is_html() -> None:
    _, body = _build()
    assert "<!DOCTYPE html>" in body
    assert "</html>" in body


def test_alert_header_present() -> None:
    _, body = _build()
    assert "MODERATION ALERT" in body


# ---------------------------------------------------------------------------
# Timestamp
# ---------------------------------------------------------------------------


def test_timestamp_date_in_body() -> None:
    ts = datetime.datetime(2026, 5, 14, 15, 42, tzinfo=_EST)
    _, body = _build(utterance_timestamp=ts)
    assert "May 14, 2026" in body


def test_timestamp_time_in_body() -> None:
    ts = datetime.datetime(2026, 5, 14, 15, 42, tzinfo=_EST)
    _, body = _build(utterance_timestamp=ts)
    assert "15:42" in body


def test_timestamp_timezone_in_body() -> None:
    ts = datetime.datetime(2026, 5, 14, 15, 42, tzinfo=_EST)
    _, body = _build(utterance_timestamp=ts)
    assert "EST" in body


def test_timestamp_appears_before_flagged_message_text() -> None:
    ts = datetime.datetime(2026, 5, 14, 15, 42, tzinfo=_EST)
    _, body = _build(utterance_timestamp=ts, utterance_text="the flagged text")
    assert body.index("May 14, 2026") < body.index("the flagged text")


def test_timestamp_utc_fallback_when_no_tzname() -> None:
    ts = datetime.datetime(2026, 5, 14, 9, 0, tzinfo=datetime.UTC)
    _, body = _build(utterance_timestamp=ts)
    assert "UTC" in body
