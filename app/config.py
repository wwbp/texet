import datetime
import os
from typing import Final, Literal


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


# OPENAI_API_KEY: API key for OpenAI.
def get_openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


# OPENAI_MODEL: OpenAI model name.
def get_openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "")


# ADMIN_USERNAME: username for SQLAdmin login.
def get_admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "")


# ADMIN_PASSWORD: password for SQLAdmin login.
def get_admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


# ADMIN_SECRET_KEY: secret for admin sessions.
def get_admin_secret_key() -> str:
    return os.getenv("ADMIN_SECRET_KEY", "")


# ADMIN_SESSION_TTL_SECONDS: admin session lifetime in seconds.
def get_admin_session_ttl_seconds() -> int:
    value = os.getenv("ADMIN_SESSION_TTL_SECONDS")
    if not value:
        return 8 * 60 * 60
    try:
        parsed = int(value)
    except ValueError:
        return 8 * 60 * 60
    return parsed if parsed >= 300 else 8 * 60 * 60


def admin_enabled() -> bool:
    return bool(get_admin_username() and get_admin_password() and get_admin_secret_key())


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

CONSOLE_PREFIX: Final[str] = "/console"
