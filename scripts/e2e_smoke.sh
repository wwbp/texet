#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the response flow.
# - Creates an API key if needed.
# - Sends a short multi-user sequence to /response.
# - Waits for queued replies to resolve.
# - Verifies DB counts scoped to this run.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env.api ]]; then
  set -a
  . ./.env.api
  set +a
fi

if [[ -f .env.db ]]; then
  set -a
  . ./.env.db
  set +a
fi

BASE_URL="${BASE_URL:-http://localhost:8000}"
DB_USER="${POSTGRES_USER:-texet}"
DB_NAME="${POSTGRES_DB:-texet}"
SMS_OUTBOUND_URL="${SMS_OUTBOUND_URL:-}"

ensure_api_key() {
  if [[ -n "${API_KEY:-}" ]]; then
    return
  fi
  if [[ -n "$(docker compose ps -q api 2>/dev/null)" ]]; then
    key_cmd=(docker compose exec -T api uv run python -m app.auth.cli --name smoke)
  else
    key_cmd=(docker compose run --rm -T api uv run python -m app.auth.cli --name smoke)
  fi
  API_KEY="$("${key_cmd[@]}" | tail -n 1 | tr -d '\r')"
  if [[ -z "${API_KEY}" ]]; then
    echo "Failed to create API key."
    exit 1
  fi
  export API_KEY
}

db_scalar() {
  docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -t -A -c "$1" \
    | tr -d '[:space:]'
}

health_code="$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")"
if [[ "$health_code" != "200" ]]; then
  echo "API not healthy: $BASE_URL/health returned $health_code"
  exit 1
fi

ensure_api_key

run_id="$(date +%s)"
u1="e2e-u1-$run_id"
u2="e2e-u2-$run_id"
bot1="bot:${u1}"
bot2="bot:${u2}"

users_sql="'${u1}','${u2}'"
bots_sql="'${bot1}','${bot2}'"

for user_id in "$u1" "$u2"; do
  for seq in 1 2; do
    message="msg-${user_id}-${seq}"
    http_code="$(
      curl -s -o /tmp/response.json -w "%{http_code}" \
        -H "Authorization: Bearer ${API_KEY}" \
        -H "Content-Type: application/json" \
        -X POST "$BASE_URL/response" \
        -d "{\"user_id\":\"${user_id}\",\"input\":\"${message}\"}"
    )"
    if [[ "$http_code" != "202" ]]; then
      echo "Request failed for ${user_id} (status ${http_code}):"
      cat /tmp/response.json
      exit 1
    fi
  done
done

deadline="$((SECONDS + 120))"
while true; do
  queued="$(db_scalar "select count(*) from utterances where status = 'queued' and speaker_id in (${bots_sql});")"
  if [[ "$queued" == "0" ]]; then
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for queued replies to send."
    exit 1
  fi
  sleep 1
done

speakers_count="$(db_scalar "select count(*) from speakers where id in (${users_sql},${bots_sql});")"
conversations_count="$(db_scalar "select count(*) from conversations where owner_speaker_id in (${users_sql});")"
user_utterances_count="$(db_scalar "select count(*) from utterances where speaker_id in (${users_sql});")"
bot_utterances_count="$(db_scalar "select count(*) from utterances where speaker_id in (${bots_sql});")"
user_received_count="$(db_scalar "select count(*) from utterances where speaker_id in (${users_sql}) and status = 'received';")"
bot_sent_count="$(db_scalar "select count(*) from utterances where speaker_id in (${bots_sql}) and status = 'sent';")"
bot_failed_count="$(db_scalar "select count(*) from utterances where speaker_id in (${bots_sql}) and status = 'failed';")"

expected_speakers=4
expected_conversations=2
expected_messages=4
expected_user_utterances="$expected_messages"
expected_bot_utterances="$expected_messages"
expected_utterances="$((expected_messages * 2))"

if [[ "$speakers_count" != "$expected_speakers" ]]; then
  echo "Unexpected speaker count: $speakers_count (expected $expected_speakers)"
  exit 1
fi
if [[ "$conversations_count" != "$expected_conversations" ]]; then
  echo "Unexpected conversation count: $conversations_count (expected $expected_conversations)"
  exit 1
fi
if [[ "$user_utterances_count" != "$expected_user_utterances" ]]; then
  echo "Unexpected user utterance count: $user_utterances_count (expected $expected_user_utterances)"
  exit 1
fi
if [[ "$bot_utterances_count" != "$expected_bot_utterances" ]]; then
  echo "Unexpected bot utterance count: $bot_utterances_count (expected $expected_bot_utterances)"
  exit 1
fi
if [[ "$user_received_count" != "$expected_user_utterances" ]]; then
  echo "Unexpected user received count: $user_received_count (expected $expected_user_utterances)"
  exit 1
fi

if [[ -z "$SMS_OUTBOUND_URL" ]]; then
  if [[ "$bot_failed_count" != "$expected_messages" ]]; then
    echo "Unexpected bot failed count: $bot_failed_count (expected $expected_messages)"
    exit 1
  fi
  if [[ "$bot_sent_count" != "0" ]]; then
    echo "Unexpected bot sent count: $bot_sent_count (expected 0)"
    exit 1
  fi
  echo "E2E OK: ${expected_messages} requests, ${expected_utterances} utterances, SMS attempts failed as expected."
else
  if [[ "$bot_failed_count" != "0" ]]; then
    echo "Unexpected bot failed count: $bot_failed_count (expected 0)"
    exit 1
  fi
  if [[ "$bot_sent_count" != "$expected_messages" ]]; then
    echo "Unexpected bot sent count: $bot_sent_count (expected $expected_messages)"
    exit 1
  fi
  echo "E2E OK: ${expected_messages} requests, ${expected_utterances} utterances, all sent."
fi

echo "Running moderation smoke..."
docker compose run --rm --build -T api env PYTHONPATH=/app uv run python scripts/moderation_smoke.py
