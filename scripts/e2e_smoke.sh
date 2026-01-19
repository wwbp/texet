#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the chat flow:
# - Sends a short, interleaved 3-user sequence to /chat.
# - Waits for queued replies to resolve.
# - Verifies DB deltas for speakers, conversations, and utterances.
# - If SMS_OUTBOUND_URL is empty, expects failed replies for SMS.

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
API_TOKEN="${API_TOKEN:-}"
if [[ -z "$API_TOKEN" ]]; then
  echo "API_TOKEN is required (set it in .env.api or env)."
  exit 1
fi

DB_USER="${POSTGRES_USER:-texet}"
DB_NAME="${POSTGRES_DB:-texet}"
SMS_OUTBOUND_URL="${SMS_OUTBOUND_URL:-}"

db_scalar() {
  docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -t -A -c "$1" \
    | tr -d '[:space:]'
}

health_code="$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")"
if [[ "$health_code" != "200" ]]; then
  echo "API not healthy: $BASE_URL/health returned $health_code"
  exit 1
fi

baseline_speakers="$(db_scalar "select count(*) from speakers;")"
baseline_conversations="$(db_scalar "select count(*) from conversations;")"
baseline_utterances="$(db_scalar "select count(*) from utterances;")"
baseline_received="$(db_scalar "select count(*) from utterances where status = 'received';")"
baseline_sent="$(db_scalar "select count(*) from utterances where status = 'sent';")"
baseline_failed="$(db_scalar "select count(*) from utterances where status = 'failed';")"
baseline_failed_sms="$(
  db_scalar "select count(*) from utterances where status = 'failed' and error = 'SMS_OUTBOUND_URL is not set.';"
)"

run_id="$(date +%s)"
u1="e2e-u1-$run_id"
u2="e2e-u2-$run_id"
u3="e2e-u3-$run_id"
order=("$u1" "$u2" "$u3" "$u2" "$u1" "$u3")

count_u1=0
count_u2=0
count_u3=0
for user_id in "${order[@]}"; do
  if [[ "$user_id" == "$u1" ]]; then
    count_u1="$((count_u1 + 1))"
    seq="$count_u1"
  elif [[ "$user_id" == "$u2" ]]; then
    count_u2="$((count_u2 + 1))"
    seq="$count_u2"
  else
    count_u3="$((count_u3 + 1))"
    seq="$count_u3"
  fi
  message="msg-${user_id}-${seq}"
  http_code="$(
    curl -s -o /tmp/chat.json -w "%{http_code}" \
      -H "Authorization: Bearer ${API_TOKEN}" \
      -H "Content-Type: application/json" \
      -X POST "$BASE_URL/chat" \
      -d "{\"user_id\":\"${user_id}\",\"message\":\"${message}\"}"
  )"
  if [[ "$http_code" != "202" ]]; then
    echo "Request failed for ${user_id} (status ${http_code}):"
    cat /tmp/chat.json
    exit 1
  fi
done

deadline="$((SECONDS + 120))"
while true; do
  queued="$(db_scalar "select count(*) from utterances where status = 'queued';")"
  if [[ "$queued" == "0" ]]; then
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for queued replies to send."
    exit 1
  fi
  sleep 1
done

after_speakers="$(db_scalar "select count(*) from speakers;")"
after_conversations="$(db_scalar "select count(*) from conversations;")"
after_utterances="$(db_scalar "select count(*) from utterances;")"
after_received="$(db_scalar "select count(*) from utterances where status = 'received';")"
after_sent="$(db_scalar "select count(*) from utterances where status = 'sent';")"
after_failed="$(db_scalar "select count(*) from utterances where status = 'failed';")"
after_failed_sms="$(
  db_scalar "select count(*) from utterances where status = 'failed' and error = 'SMS_OUTBOUND_URL is not set.';"
)"

delta_speakers="$((after_speakers - baseline_speakers))"
delta_conversations="$((after_conversations - baseline_conversations))"
delta_utterances="$((after_utterances - baseline_utterances))"
delta_received="$((after_received - baseline_received))"
delta_sent="$((after_sent - baseline_sent))"
delta_failed="$((after_failed - baseline_failed))"
delta_failed_sms="$((after_failed_sms - baseline_failed_sms))"

expected_messages="${#order[@]}"
expected_speakers=6
expected_conversations=3
expected_utterances="$((expected_messages * 2))"
expected_received="$expected_messages"
expected_sent="$expected_messages"

if [[ "$delta_speakers" != "$expected_speakers" ]]; then
  echo "Unexpected speaker delta: $delta_speakers (expected $expected_speakers)"
  exit 1
fi
if [[ "$delta_conversations" != "$expected_conversations" ]]; then
  echo "Unexpected conversation delta: $delta_conversations (expected $expected_conversations)"
  exit 1
fi
if [[ "$delta_utterances" != "$expected_utterances" ]]; then
  echo "Unexpected utterance delta: $delta_utterances (expected $expected_utterances)"
  exit 1
fi
if [[ "$delta_received" != "$expected_received" ]]; then
  echo "Unexpected received delta: $delta_received (expected $expected_received)"
  exit 1
fi
if [[ -z "$SMS_OUTBOUND_URL" ]]; then
  if [[ "$delta_failed" != "$expected_messages" ]]; then
    echo "Unexpected failed delta: $delta_failed (expected $expected_messages)"
    exit 1
  fi
  if [[ "$delta_failed_sms" != "$expected_messages" ]]; then
    echo "Unexpected SMS failure delta: $delta_failed_sms (expected $expected_messages)"
    exit 1
  fi
  if [[ "$delta_sent" != "0" ]]; then
    echo "Unexpected sent delta: $delta_sent (expected 0)"
    exit 1
  fi
  echo "E2E OK: ${expected_messages} requests, ${expected_utterances} utterances, SMS attempts failed as expected."
else
  if [[ "$delta_failed" != "0" ]]; then
    echo "Unexpected failed delta: $delta_failed (expected 0)"
    exit 1
  fi
  if [[ "$delta_sent" != "$expected_sent" ]]; then
    echo "Unexpected sent delta: $delta_sent (expected $expected_sent)"
    exit 1
  fi
  echo "E2E OK: ${expected_messages} requests, ${expected_utterances} utterances, all sent."
fi
