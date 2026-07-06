import datetime
import os
from typing import Final, Literal


# SMS_OUTBOUND_URL: webhook to deliver outbound SMS replies.
def get_sms_outbound_url() -> str:
    return os.getenv("SMS_OUTBOUND_URL", "")


# SMS_OUTBOUND_AUTHORIZATION: Authorization header value for outbound SMS requests.
def get_sms_outbound_authorization() -> str:
    return os.getenv("SMS_OUTBOUND_AUTHORIZATION", "")


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


def get_mail_username() -> str:
    return os.getenv("MAIL_USERNAME", "")


def get_mail_password() -> str:
    return os.getenv("MAIL_PASSWORD", "")


def get_mail_from() -> str:
    return os.getenv("MAIL_FROM", "")


def get_mail_port() -> int:
    value = os.getenv("MAIL_PORT")
    if not value:
        return 465
    try:
        parsed = int(value)
    except ValueError:
        return 465
    return parsed if parsed > 0 else 465


def get_mail_server() -> str:
    return os.getenv("MAIL_SERVER", "")


def get_mail_starttls() -> bool:
    return os.getenv("MAIL_STARTTLS", "").strip().lower() in {"1", "true", "yes", "on"}


def get_mail_ssl_tls() -> bool:
    value = os.getenv("MAIL_SSL_TLS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_mail_use_credentials() -> bool:
    value = os.getenv("MAIL_USE_CREDENTIALS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_mail_validate_certs() -> bool:
    value = os.getenv("MAIL_VALIDATE_CERTS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_moderation_alert_emails() -> list[str]:
    raw = os.getenv("MODERATION_ALERT_EMAILS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


# PUBLIC_APP_URL: base URL used in moderation alert emails for admin links.
# Example: https://texet.example.com  (no trailing slash)
def get_public_app_url() -> str:
    return os.getenv("PUBLIC_APP_URL", "").rstrip("/")


# OPENAI_API_KEY: API key for OpenAI.
def get_openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def get_aws_access_key_id() -> str:
    return os.getenv("AWS_ACCESS_KEY_ID", "")


def get_aws_secret_access_key() -> str:
    return os.getenv("AWS_SECRET_ACCESS_KEY", "")


def get_aws_region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


BEDROCK_DEFAULT_MODEL: Final[str] = "us.anthropic.claude-sonnet-4-6"


# OPENAI_MODEL: OpenAI model name.
def get_openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "")


# MOCK_EXTERNAL_APIS: replace LLM generation, moderation, and outbound SMS with
# in-process fakes that simulate latency. Load testing only — never set in production.
def mock_external_apis() -> bool:
    return os.getenv("MOCK_EXTERNAL_APIS", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_latency_ms(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


# MOCK_LLM_LATENCY_MS: simulated LLM generation latency when MOCK_EXTERNAL_APIS is on.
def get_mock_llm_latency_ms() -> int:
    return _get_latency_ms("MOCK_LLM_LATENCY_MS", 1500)


# MOCK_MODERATION_LATENCY_MS: simulated moderation latency when MOCK_EXTERNAL_APIS is on.
def get_mock_moderation_latency_ms() -> int:
    return _get_latency_ms("MOCK_MODERATION_LATENCY_MS", 300)


# MOCK_SMS_LATENCY_MS: simulated outbound SMS latency when MOCK_EXTERNAL_APIS is on.
def get_mock_sms_latency_ms() -> int:
    return _get_latency_ms("MOCK_SMS_LATENCY_MS", 150)


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
UTTERANCE_STATUS_MODERATED: Final[Literal["moderated"]] = "moderated"
UTTERANCE_STATUS_FAILED: Final[Literal["failed"]] = "failed"

UTTERANCE_STATUSES = (
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_SENT,
    UTTERANCE_STATUS_MODERATED,
    UTTERANCE_STATUS_FAILED,
)

UTTERANCE_STATUSES_SQL = ", ".join(f"'{status}'" for status in UTTERANCE_STATUSES)

DEFAULT_TIMEZONE_NAME: Final[str] = "EST"
DEFAULT_TIMEZONE: Final[datetime.tzinfo] = datetime.timezone(
    datetime.timedelta(hours=-5), name=DEFAULT_TIMEZONE_NAME
)

CONSOLE_PREFIX: Final[str] = "/console"

MODERATION_VALUES_FOR_BLOCKED = {
    "harassment": 0.5,
    "harassment/threatening": 0.1,
    "hate": 0.5,
    "hate/threatening": 0.1,
    "self-harm": 0.2,
    "self-harm/instructions": 0.5,
    "self-harm/intent": 0.7,
    "sexual": 0.5,
    "sexual/minors": 0.2,
    "violence": 0.7,
    "violence/graphic": 0.8,
}
