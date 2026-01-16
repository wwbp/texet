import os
from typing import Final, Literal


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None else default


def _get_int_env(name: str, default: int, minimum: int | None = None) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _get_float_env(name: str, default: float, minimum: float | None = None) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


# API_TOKEN: bearer auth for /chat.
def get_api_token() -> str:
    return _get_env("API_TOKEN", "")


# SMS_OUTBOUND_URL: webhook to deliver outbound SMS replies.
def get_sms_outbound_url() -> str:
    return _get_env("SMS_OUTBOUND_URL", "")


# SMS_TIMEOUT_SECONDS: outbound HTTP timeout in seconds.
def get_sms_timeout_seconds() -> float:
    return _get_float_env("SMS_TIMEOUT_SECONDS", 10.0, minimum=0.1)


# MESSAGE_MIN_LENGTH: minimum characters for inbound/outbound messages.
MESSAGE_MIN_LENGTH = _get_int_env("MESSAGE_MIN_LENGTH", 1, minimum=1)

# MESSAGE_MAX_LENGTH: maximum characters for inbound/outbound messages.
MESSAGE_MAX_LENGTH = _get_int_env("MESSAGE_MAX_LENGTH", 4000, minimum=MESSAGE_MIN_LENGTH)

UTTERANCE_STATUS_RECEIVED: Final[Literal["received"]] = "received"
UTTERANCE_STATUS_QUEUED: Final[Literal["queued"]] = "queued"
UTTERANCE_STATUS_SENT: Final[Literal["sent"]] = "sent"
UTTERANCE_STATUS_FAILED: Final[Literal["failed"]] = "failed"

UTTERANCE_STATUSES = (
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_SENT,
    UTTERANCE_STATUS_FAILED,
)

UTTERANCE_STATUSES_SQL = ", ".join(f"'{status}'" for status in UTTERANCE_STATUSES)
