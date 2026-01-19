import datetime
import os
from typing import Final, Literal


# API_TOKEN: bearer auth for /chat.
def get_api_token() -> str:
    return os.getenv("API_TOKEN", "")


# SMS_OUTBOUND_URL: webhook to deliver outbound SMS replies.
def get_sms_outbound_url() -> str:
    return os.getenv("SMS_OUTBOUND_URL", "")


# SMS_TIMEOUT_SECONDS: outbound HTTP timeout in seconds.
def get_sms_timeout_seconds() -> float:
    value = os.getenv("SMS_TIMEOUT_SECONDS")
    if not value:
        return 15.0
    try:
        parsed = float(value)
    except ValueError:
        return 15.0
    return parsed if parsed >= 0.1 else 15.0


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

DEFAULT_TIMEZONE_NAME: Final[str] = "EST"
DEFAULT_TIMEZONE: Final[datetime.tzinfo] = datetime.timezone(
    datetime.timedelta(hours=-5), name=DEFAULT_TIMEZONE_NAME
)
