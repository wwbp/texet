# -----------------------------------------------------------------------------
# locals.tf — computed values reused across the stack.
# -----------------------------------------------------------------------------

locals {
  name_prefix = var.name_prefix

  common_tags = {
    Project     = "texet"
    Environment = "perf"
    ManagedBy   = "terraform"
    Purpose     = "load-testing"
  }

  availability_zones = [
    "${var.aws_region}a",
    "${var.aws_region}b",
  ]

  db_name     = "texet"
  db_username = "texet"

  # The app and Alembic both use the async URL scheme (alembic/env.py, .env.api).
  # A single DATABASE_URL therefore drives both migrations and the running app.
  database_url = "postgresql+asyncpg://${local.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:5432/${local.db_name}"

  # Environment shared by both the api and worker tasks.
  common_env = {
    DATABASE_URL               = local.database_url
    MOCK_EXTERNAL_APIS         = "true"
    MOCK_LLM_LATENCY_MS        = tostring(var.mock_llm_latency_ms)
    MOCK_MODERATION_LATENCY_MS = tostring(var.mock_moderation_latency_ms)
    MOCK_SMS_LATENCY_MS        = tostring(var.mock_sms_latency_ms)
    DB_POOL_SIZE               = tostring(var.db_pool_size)
    DB_MAX_OVERFLOW            = tostring(var.db_max_overflow)
  }
}
