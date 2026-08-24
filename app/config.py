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


# Every path that generates text — replies and weekly summaries — runs on
# Bedrock Llama. The openai provider stays selectable in the console, but it
# is no longer any code path's default: an OpenAI default is only ever
# reached by falling back, which is exactly when nobody is watching.
DEFAULT_LLM_PROVIDER: Final[str] = "bedrock"
BEDROCK_DEFAULT_MODEL: Final[str] = "us.meta.llama4-maverick-17b-instruct-v1:0"


# OPENAI_MODEL: OpenAI model name.
def get_openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "")


# MOCK_EXTERNAL_APIS: replace LLM generation, moderation, and outbound SMS with
# in-process fakes that simulate latency. Load testing only — never set in production.
def mock_external_apis() -> bool:
    return os.getenv("MOCK_EXTERNAL_APIS", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_non_negative_int(name: str, default: int) -> int:
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
    return _get_non_negative_int("MOCK_LLM_LATENCY_MS", 1500)


# MOCK_MODERATION_LATENCY_MS: simulated moderation latency when MOCK_EXTERNAL_APIS is on.
def get_mock_moderation_latency_ms() -> int:
    return _get_non_negative_int("MOCK_MODERATION_LATENCY_MS", 300)


# MOCK_SMS_LATENCY_MS: simulated outbound SMS latency when MOCK_EXTERNAL_APIS is on.
def get_mock_sms_latency_ms() -> int:
    return _get_non_negative_int("MOCK_SMS_LATENCY_MS", 150)


# MAX_QUEUE_DEPTH: /response returns 503 while queued+processing replies exceed
# this count. 0 disables backpressure.
def get_max_queue_depth() -> int:
    return _get_non_negative_int("MAX_QUEUE_DEPTH", 1000)


# WORKER_CONCURRENCY: concurrent claim/process loops per worker process.
def get_worker_concurrency() -> int:
    value = _get_non_negative_int("WORKER_CONCURRENCY", 20)
    return value if value > 0 else 20


# WORKER_POLL_INTERVAL_SECONDS: fallback sleep when the queue is empty. Idle
# workers normally wake on a LISTEN/NOTIFY signal (app.queue.NOTIFY_CHANNEL);
# this interval only bounds latency for missed notifications and reclaimed items.
def get_worker_poll_interval_seconds() -> float:
    value = os.getenv("WORKER_POLL_INTERVAL_SECONDS")
    if not value:
        return 2.0
    try:
        parsed = float(value)
    except ValueError:
        return 2.0
    return parsed if parsed >= 0.05 else 2.0


# WORKER_RECLAIM_SECONDS: visibility timeout after which a stale 'processing'
# claim is returned to the queue (or failed once out of attempts).
def get_worker_reclaim_seconds() -> int:
    value = _get_non_negative_int("WORKER_RECLAIM_SECONDS", 300)
    return value if value > 0 else 300


# WORKER_MAX_ATTEMPTS: claim attempts before a reply is marked failed.
def get_worker_max_attempts() -> int:
    value = _get_non_negative_int("WORKER_MAX_ATTEMPTS", 3)
    return value if value > 0 else 3


# SCHEDULER_ENABLED: run the APScheduler cron jobs in this API process. Set to
# false on all but one replica when running multiple API instances.
def scheduler_enabled() -> bool:
    value = os.getenv("SCHEDULER_ENABLED")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
UTTERANCE_STATUS_PROCESSING: Final[Literal["processing"]] = "processing"
UTTERANCE_STATUS_SENT: Final[Literal["sent"]] = "sent"
UTTERANCE_STATUS_MODERATED: Final[Literal["moderated"]] = "moderated"
UTTERANCE_STATUS_FAILED: Final[Literal["failed"]] = "failed"

UTTERANCE_STATUSES = (
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_PROCESSING,
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

# Score above which a message is withheld, per moderation category.
#
# The study moderates for two families only: self-harm and sexual content. The
# rest are pinned to 1.0, which is an off switch rather than a high bar — the
# comparison is a strict '>' and moderation scores are bounded at 1.0, so no
# score clears it. That is the same value already applied to any category the
# API reports that is not listed here.
#
# They are kept as explicit entries instead of being deleted so the full set the
# API returns stays visible, and so re-enabling one is a threshold edit rather
# than a rediscovery of the category's name.
MODERATION_VALUES_FOR_BLOCKED = {
    # Enforced.
    "self-harm": 0.2,
    "self-harm/instructions": 0.5,
    "self-harm/intent": 0.7,
    "sexual": 0.5,
    "sexual/minors": 0.2,
    # Not enforced.
    "harassment": 1.0,
    "harassment/threatening": 1.0,
    "hate": 1.0,
    "hate/threatening": 1.0,
    "violence": 1.0,
    "violence/graphic": 1.0,
}
