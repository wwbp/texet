# Load testing & benchmark notes

Benchmarks of `POST /response` with all external APIs mocked, on a local
docker-compose stack (postgres:16-alpine). Locust profile: **500 concurrent
users at ~100 rps** (`constant_throughput(0.2)` per user, 10:1 message/health
task split → ~91 rps on `/response`), 5-minute runs, 10s client timeout.

Three rounds: rounds 1–2 (2026-07-06) tuned a single-process design to its
limits; round 3 (2026-07-07) split reply generation into a separate worker
service backed by a Postgres queue, adding horizontal scaling and backpressure.
Jump to [Round 3](#round-3-postgres-work-queue--backpressure) for the current
architecture.

## How to reproduce

```bash
# 1. Start the stack in perf mode (mocks externals; api + worker + db).
#    Scale reply capacity with worker replicas and concurrency:
PERF_WORKER_CONCURRENCY=80 \
  docker compose -f docker-compose.yml -f docker-compose.perf.yml up --build -d --scale worker=3

# 2. Create a key
make api-key   # export the printed key

# 3. Run the benchmark (writes CSVs + HTML report to perf-results/)
TEXET_API_KEY=... make load-perf              # defaults: USERS=500, DURATION=5m

# Watch the queue drain / workers claim:
make worker-logs
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

## Round 3: Postgres work queue + backpressure

Round 2 removed the connection-hold and missing-index problems but left two
structural limits: one uvicorn worker CPU-saturated at ~100–160 msg/s (no way
to add reply capacity), and `/response` accepted without bound so run 4 ended
with a 14k backlog that a restart would strand. Round 3 addresses both.

**Architecture change.** Reply generation moved out of the API's in-process
`BackgroundTasks` into a separate **worker service** (`app/worker.py`,
`python -m app.worker`). Queued bot utterances *are* the queue; workers claim
them with `FOR UPDATE SKIP LOCKED` (`app/queue.py`). A claim takes a user's
oldest queued reply only if that user has nothing in `processing`, which
preserves per-user ordering across any number of workers — replacing the
single-process `asyncio.Lock`. `/response` is now accept-only and returns
**503 + Retry-After** once queued+processing replies reach `MAX_QUEUE_DEPTH`
(default 1000, 0 disables). Stale claims are reclaimed after a visibility
timeout, and because workers pull whatever is `queued`, a restart re-drains
automatically — no user re-message needed (verified: message queued with the
worker stopped went `sent` 6 s after restart).

### Run 5 — under-provisioned (1 worker, concurrency 40), 500 users / 100 rps: backpressure engages

At 2.25 s mock latency, one worker with 40 slots tops out at ~18 replies/s, far
below the ~91 rps offered. Backpressure did exactly its job:

| Metric | Value |
|---|---|
| `/response` accepted | 21% (p50 480 ms, p95 1.4 s — fast and stable) |
| `/response` 503s | 16,388 (queue held at the 1000 cap, not growing unbounded) |
| Backlog | pinned at ~1000, never runaway |
| Pool errors | 0 |

The system stayed healthy under 5× overload — it shed excess load at the door
instead of accumulating a backlog it couldn't finish (contrast run 4's 14k).

### Run 5b — provisioned (3 workers, concurrency 80 = 240 slots), 500 users / 100 rps: clean

| Metric | Run 4 (before) | Run 5b (after) |
|---|---|---|
| Throughput | 179 rps / 10% fail | **100 rps / 0.02% fail** |
| `/response` p50 / p95 / p99 | 660 ms / 9.4 s / — | **570 ms / 670 ms / 760 ms** |
| 503s | n/a | **0** (backlog stayed under cap) |
| Replies completed | 14k **stranded** | **26,618 / 26,618 sent, 0 failed** |
| Backlog after load stopped | — | **drained to 0** (no stranding) |

The API stayed flat and fast (max 935 ms) because it only does inserts now;
reply work is entirely on the workers. This is the target the single-worker
architecture could not hit.

**New ceiling found: Postgres CPU.** With the app-CPU limit gone, DB CPU became
the bottleneck — it climbed to ~150% (1.5 cores) and backlog crept 270 → 464
over the run (still under the cap, hence zero 503s). The cost is the claim query
polled continuously across 240 worker coroutines plus per-reply context reads.
Worker CPU was uneven (one replica often near-idle) because idle pollers lose
`SKIP LOCKED` races. Both point to the same fix below.

## Round 4: on AWS (ECS Fargate, mock mode)

Same code, deployed to real infra via the Terraform in `infra/terraform/` — an
isolated stack (own VPC + RDS Postgres, ALB, ECR) with **api and worker as
separate Fargate services**. Mock mode on (`MOCK_EXTERNAL_APIS=true`). Load
driven from a laptop against the ALB. This surfaced things the local runs
couldn't: a real load balancer, network hops, and Fargate vCPU sizing.

### AWS run 1 — 1 api task @ 0.5 vCPU, 3 workers @ concurrency 80: api starved

| Metric | Result |
|---|---|
| Throughput | ~17.5 rps (target ~91) |
| Failures | 43.6% — client **10 s timeouts** |
| `/response` p50 / p95 | 6.2 s / 10 s |
| api CPU | ~0.4% (idle) yet `/health` slow (p50 370 ms) |
| worker CPU | ~90% ; RDS ~63% |

Diagnosis: a single 0.5-vCPU api task couldn't accept 500 concurrent
connections behind the ALB — requests queued and timed out while the task
itself sat idle (connection-bound, not CPU-bound). The load never really
reached the queue. **A provisioning gap that only shows up on real infra** —
locally the api had a full fast core and no load balancer.

### AWS run 2 — 3 api tasks @ 1 vCPU (migrations moved off the api), workers @ concurrency 40

| Metric | Run 1 | Run 2 |
|---|---|---|
| Accepted throughput | 17.5 rps | **97.5 rps aggregate / 88.6 on `/response`** (≈ target) |
| `/response` p50 / p95 / p99 | 6.2 s / 10 s / — | **340 ms / 850 ms / 2.7 s** |
| `/health` p50 | 370 ms | **40 ms** |
| Failures | 43.6% timeouts | **40.3%, all clean 503 backpressure** (zero timeouts) |
| api / worker / RDS CPU | 0.4% / 90% / 63% | **~50% / ~24% / ~62%** (all headroom) |

Scaling the api tier fixed the starvation: throughput 17→**97 rps**, p50
6.2 s→**340 ms**, and — importantly — the failures **converted from timeouts
(collapse) to 503s (intentional load-shedding)**. The api now accepts fast and
sheds excess cleanly instead of hanging.

Why 40% 503s: I over-corrected worker concurrency down to 40 (3 × 40 = 120
reply slots; at 2.25 s mock latency that caps completion at ~53 replies/s,
below the ~89 rps offered), so the queue hit `MAX_QUEUE_DEPTH` and shed the
rest. Worker CPU was only ~24% — they're **connection/slot-bound, not
CPU-bound** — so the fix is simply more reply slots.

### AWS run 3 — workers @ concurrency 80 (3 × 80 = 240 slots): DB becomes the ceiling

| Metric | Run 2 (120 slots) | Run 3 (240 slots) |
|---|---|---|
| Accepted throughput | 88.6 rps | **88.4 rps on `/response`** (97.4 agg) |
| 503 shed | 40.3% | **8.3%** |
| `/response` p50 / p95 / p99 | 340 ms / 850 ms / 2.7 s | **260 ms / 1.4 s / 3.9 s** |
| `/health` p50 | 40 ms | **28 ms** |
| timeouts | 0 | **1** |
| api / worker CPU | 50% / 24% | ~36% / ~55% |
| **RDS CPU** | ~62% | **78–91% (the ceiling)** |

Doubling worker slots cut shedding from 40% → **8%**, and the remaining 8% is
because **RDS CPU hit ~91%** — the api (~36%) and workers (~55%) both had
headroom, so Postgres is now the bottleneck. This is the **same conclusion as
local run 5b, reproduced on real infra**: with the api and worker tiers sized to
the load, the DB is the ultimate ceiling, and the small overflow is shed as
clean 503s (p50 stayed 260 ms, one timeout).

**Net across the three AWS runs:** the architecture behaves exactly as designed
on Fargate. Run 1 → 2 (scale the api) removed connection starvation
(17 → 97 rps, timeouts → 503s); run 2 → 3 (scale worker slots) cut load-shedding
40% → 8% and exposed **Postgres CPU as the real ceiling**. The knobs that matter,
in order: api task count/size (connection capacity), worker slots (reply
capacity), then the DB — which is where the `LISTEN/NOTIFY` / claim-query
optimization from round 3 would pay off to push past ~90 rps on this instance.

Migration note: with >1 api task, the api tasks set `SKIP_MIGRATIONS=true` and
`alembic upgrade head` runs as a one-off ECS task, so multiple api tasks don't
race on startup.

## Scaling guidance for deployment

Shipping now (round 3): worker service, `FOR UPDATE SKIP LOCKED` queue, 503
backpressure, automatic restart re-drain. Deploy as **1 api + N workers**
(`docker compose ... up --scale worker=N`); scale reply capacity with worker
count and `WORKER_CONCURRENCY`, and size `MAX_QUEUE_DEPTH` to the largest
backlog you're willing to let build before shedding load.

Remaining, in priority order:

1. **Postgres CPU is the next ceiling (~100 rps on one DB core-and-a-half).**
   The dominant cost is the claim query polled by every worker coroutine.
   **Done:** idle workers now sleep on `LISTEN/NOTIFY` (`app/queue.py`
   `NOTIFY_CHANNEL`; the API notifies on enqueue) with only a slow fallback
   poll, so an idle/light-traffic fleet issues ~no claim queries instead of
   `workers × 1/poll_interval` per second. Remaining DB levers for pushing past
   ~100 rps under sustained load: simplify the claim query, and scale the DB
   (bigger instance / read replica for context reads).
2. **Tune worker fleet to offered load.** Required reply slots ≈ target msg/s ×
   external-latency seconds (use observed p95 LLM latency). Spread across
   replicas; keep each worker's `DB_POOL_SIZE`+overflow above its steady-state
   active-connection count (well under `WORKER_CONCURRENCY`, since each reply
   holds a connection only briefly).
3. **Multiple instances/replicas are safe.** Reply processing coordinates
   through the Postgres queue (no per-process lock), and the weekly-summary
   scheduler is leader-elected via a Postgres advisory lock
   (`app/scheduler.py`) so it runs on exactly one instance regardless of count
   — no per-instance config needed. Size each instance's `DB_POOL_SIZE` +
   `DB_MAX_OVERFLOW` so `instances × (pool + overflow)` stays under the RDS
   `max_connections` budget.
4. **Consider capping chat history** at the N most recent messages instead of
   the full week — reads are indexed and cheap, but prompt size (LLM
   cost/latency) still grows linearly with conversation length.

## Cleanup

Load-test rows are prefixed for removal:

```sql
DELETE FROM utterances WHERE conversation_id IN
  (SELECT id FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%');
DELETE FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%';
DELETE FROM speakers WHERE id LIKE 'loadtest-%' OR id LIKE 'bot:loadtest-%';
```
