# Load testing & benchmark notes

Benchmarks of `POST /response` with all external APIs mocked, run 2026-07-06 on a
local docker-compose stack (single `api` container, single uvicorn worker,
postgres:16-alpine). Locust profile: **500 concurrent users at ~100 rps**
(`constant_throughput(0.2)` per user, 10:1 message/health task split → ~91 rps
on `/response`), 5-minute runs, 10s client timeout.

## How to reproduce

```bash
# 1. Start the stack in perf mode (mocks externals, widens the DB pool)
docker compose -f docker-compose.yml -f docker-compose.perf.yml up --build -d

# 2. Create a key
make api-key   # export the printed key

# 3. Run the benchmark (writes CSVs + HTML report to perf-results/)
TEXET_API_KEY=... make load-perf              # defaults: USERS=500, DURATION=5m
```

`MOCK_EXTERNAL_APIS=true` replaces the three external calls in
`app/response/service.py` with `asyncio.sleep` fakes so the benchmark measures
*our* infrastructure, not OpenAI/Bedrock/SMS-hub latency. Simulated latencies
(tunable via env, see `docker-compose.perf.yml`):

| Call | Env var | Default |
|---|---|---|
| LLM generation | `MOCK_LLM_LATENCY_MS` | 1500 ms |
| Moderation (×2: user input + bot reply) | `MOCK_MODERATION_LATENCY_MS` | 300 ms |
| Outbound SMS | `MOCK_SMS_LATENCY_MS` | 150 ms |

Total simulated external latency per message: **~2.25 s**.

## The bottleneck: DB connections held across external calls

`_drain_user_queue` ([service.py:641](../app/response/service.py#L641)) opens a
DB session and holds its pooled connection for the *entire* reply pipeline —
moderation → LLM → reply moderation → SMS. So each in-flight message pins a
connection for the full external-latency window, and by Little's law:

```
sustainable msg/s ≈ (pool_size + max_overflow) / connection-hold-seconds
```

At ~91 msg/s × 2.25 s hold, steady-state demand is **~205 concurrent
connections** (plus short-lived auth/handler sessions — every `/response`
request also does a SELECT + COMMIT on `api_keys` from a second session).

The prior defaults — pool 5 + overflow 10 = 15 connections, unconfigurable —
support only ~6–7 msg/s at production LLM latencies. That is why the earlier
100-user / 20-rps tests were already near the edge.

## Results

### Run 1 — pool 20 + overflow 30 (50 connections): collapse

| Metric | Value |
|---|---|
| Requests | 15,302 |
| Failures | 12,837 (83.9%; `/response` 92.5%) |
| Effective throughput | 51.6 rps (target ~100) |
| `/response` p50 / p95 / p99 | 10,000 ms (client-timeout ceiling) |
| `/health` p50 | 10 ms |
| api CPU / mem | 20–90% of one core / ~430 MiB |
| Server errors | 2,996 × `QueuePool limit of size 20 overflow 30 reached, timeout 30.00` |

Diagnosis: pure pool starvation. `/health` (no DB) stayed at 10 ms while every
DB-touching request queued behind background drains holding connections. CPU
and memory were never the constraint.

### Run 2 — pool 100 + overflow 150 (250 connections), postgres max_connections=300

| Metric | Value |
|---|---|
| Requests | 25,010 |
| Failures | 281 (1.12%, all client-side 10s timeouts in the final minute) |
| Sustained throughput | 100 rps mid-run (71.8 rps averaged over ramp + tail) |
| `/response` p50 / p95 / p99 | 230 ms / 1,200 ms / 4,100 ms |
| Background pipeline | 22,991 messages received, **22,991 replies sent — zero backlog, zero failed** |
| Pool errors | 0 |
| Postgres client connections | pinned at 251 (pool fully utilized, as predicted) |

The system held the full 100 rps with sub-second p95 for the first ~3 minutes.
It then degraded — not from connections, but from **Postgres CPU growing
superlinearly** as conversations lengthened:

| Minute | api CPU | db CPU |
|---|---|---|
| 1 | 52% | 60% |
| 2 | 58% | 92% |
| 3 | 67% | 149% |
| 4 | 75% | 233% |

Each reply rebuilds the full chat history since the start of the week
(`build_chat_history`), so per-message DB work grows linearly with conversation
length — at 500 users × ~11 messages/min each, read volume compounds fast. The
one-week window bounds this in production (real users won't send 11 msg/min for
hours), but it is the next ceiling after connections.

## Scaling guidance for deployment

1. **Size the DB pool to the throughput target.** Required connections ≈
   target msg/s × external-latency seconds (use observed p95 LLM latency, not
   the mean). Set `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (new env vars, default
   5/10) and raise Postgres `max_connections` (or use RDS Proxy/pgbouncer —
   but note session-level pooling is required, transactions span awaits).
2. **The real fix is architectural: don't hold a connection across external
   calls.** Release the session before the LLM call and reopen it to persist
   the reply. That collapses connection demand from ~205 to ~10–20 at 100 rps
   and removes the need for a 300-connection Postgres. Worth doing before any
   horizontal scale-out.
3. **Single-replica constraint still stands.** Per-user serialization is an
   in-process `asyncio.Lock`, and background work is FastAPI `BackgroundTasks`
   in the web process — scaling to multiple replicas/workers needs a shared
   queue (e.g. Postgres `SELECT ... FOR UPDATE SKIP LOCKED` on the queued
   utterances, or SQS) before it is safe.
4. **CPU headroom is fine at this scale.** One uvicorn worker peaked below one
   core at ~90 msg/s with mocked externals; the event loop is not the
   bottleneck at 100 rps.
5. **Auth adds 2 DB round-trips per request** (SELECT on `api_keys` + COMMIT
   for `last_used_at`). At sustained high rps consider caching key hashes or
   batching the timestamp update.
6. **Chat-history rebuild is the next ceiling — and `utterances` has no
   indexes.** Run 2 degraded after ~3 minutes as per-reply history reads grew
   with conversation length (db CPU 60% → 233% over 4 minutes). Verified in
   Postgres: the only index on `utterances` is its primary key, so every
   history rebuild and every queued-reply poll
   (`speaker_id + status = 'queued'`) is a sequential scan over the whole
   table. Before scaling up, add indexes on
   `utterances (conversation_id, timestamp)` and
   `utterances (speaker_id, status)` (via `make migration`), and consider
   capping history at the N most recent messages instead of the full week.

## Cleanup

Load-test rows are prefixed for removal:

```sql
DELETE FROM utterances WHERE conversation_id IN
  (SELECT id FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%');
DELETE FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%';
DELETE FROM speakers WHERE id LIKE 'loadtest-%' OR id LIKE 'bot:loadtest-%';
```
