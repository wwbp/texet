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

*(Fixed in round 2 below — this section documents the behavior behind runs 1–2.)*

`_drain_user_queue` opened a
DB session and held its pooled connection for the *entire* reply pipeline —
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

## Round 2: design fixes + scaling further (same day)

Three fixes landed after runs 1–2, targeting the root causes above:

1. **Sessions are no longer held across external calls.**
   `_process_queued_reply` now reads all prompt/history context in one
   short-lived session, runs moderation/LLM with no connection held, and opens
   a fresh session to persist (SMS still sends before commit, preserving the
   rollback-on-delivery-failure semantics). Connection hold per message dropped
   from ~2.25 s to ~150 ms.
2. **Indexes added** (migration `0ae531a57aaa`):
   `utterances (conversation_id, timestamp)` and `(speaker_id, status)`.
3. **Auth timestamp throttled**: `last_used_at` is refreshed at most once per
   60 s instead of a write + commit on every request.

### Run 3 — the run-1 collapse config (pool 20+30, 500 users / 100 rps): passes

| Metric | Run 1 (before) | Run 3 (after) |
|---|---|---|
| Failure rate | 83.9% | **0.52%** |
| `/response` p50 | 10,000 ms | **430 ms** |
| `/response` p95 | 10,000 ms | **800 ms** |
| Throughput | 51.6 rps | **97.5 rps** |
| `QueuePool` errors | 2,996 | **0** |
| db CPU trend | 60% → 233% (superlinear) | **flat ~40%** |

Identical load, identical pool. The residual 0.5% failures correlate with api
CPU peaking at ~104% of one core — the single uvicorn worker, not the DB, is
now the limit.

### Run 4 — 1000 users / 200 rps (pool 40+60): single-worker CPU ceiling

| Metric | Value |
|---|---|
| Throughput | 179.6 rps of 200 target |
| Failure rate | 10.2% (client 10s timeouts) |
| `/response` p50 / p95 | 660 ms / 9,400 ms |
| api CPU | pegged 100% of one core for the entire run |
| db CPU | ~45% (healthy) |
| Locust client CPU | 16–21% (not a confound) |
| `QueuePool` errors | 54 (pool 100 slightly tight) |
| Background queue | fell behind: 14,252 replies still queued at test end |

Two conclusions. First, **one uvicorn worker saturates between ~100 and
~160 msg/s** — beyond that, capacity must come from more workers/replicas,
which is blocked on the shared-queue work below. Second, there is **no
backpressure**: `/response` keeps 202-accepting while the reply queue grows
without bound, and a restart strands queued utterances — nothing re-drains
them until that user happens to message again.

## Scaling guidance for deployment

Done in round 2 (see above): sessions released around external calls, indexes
on `utterances`, throttled auth writes. Remaining, in priority order:

1. **Up to ~100 msg/s: deploy as-is with a modest pool.** One replica with
   `DB_POOL_SIZE=20` / `DB_MAX_OVERFLOW=30` handled 500 users / 100 rps with
   p95 800 ms. Connection demand is now ≈ msg/s × 0.15 s (the persist+SMS
   window — SMS deliberately sends inside the transaction) plus context reads;
   size the pool with headroom above that and verify with a run 3-style test.
2. **Beyond ~100 msg/s: scale out workers/replicas — which first needs a
   shared queue.** One uvicorn worker CPU-saturates at ~100–160 msg/s. Both
   the per-user `asyncio.Lock` and `BackgroundTasks` are in-process, so
   multiple workers/replicas are unsafe until queued utterances are claimed
   via a shared mechanism (Postgres `SELECT ... FOR UPDATE SKIP LOCKED`, or
   SQS + a worker service).
3. **Add backpressure and a re-drain path.** `/response` 202-accepts
   regardless of queue depth (run 4 ended with a 14k backlog), and queued
   utterances stranded by a deploy/restart are never picked up until that user
   messages again. Options: reject/429 above a queue-depth threshold, and on
   startup (or on a timer) kick `_drain_user_queue` for users with queued
   utterances.
4. **Consider capping chat history** at the N most recent messages instead of
   the full week — with indexes the reads are cheap, but prompt size still
   grows linearly with conversation length (LLM cost/latency, not DB, at
   realistic volumes).

## Cleanup

Load-test rows are prefixed for removal:

```sql
DELETE FROM utterances WHERE conversation_id IN
  (SELECT id FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%');
DELETE FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%';
DELETE FROM speakers WHERE id LIKE 'loadtest-%' OR id LIKE 'bot:loadtest-%';
```
