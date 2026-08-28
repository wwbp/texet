# SMART-R — System Architecture

> **Audience:** Engineering team / new contributors
> **Last updated:** 2026-08-25
> **Scope:** How the hub and texet divide the work, what each owns, and the two HTTP calls between them.
> **Diagrams:** [`architecture.tex`](architecture.tex) → `architecture.pdf` — layering, architecture, infrastructure, data flow.

---

## The two services

Two separate FastAPI applications, two separate Postgres databases, two separate Elastic
Beanstalk applications. They know about each other only through the two HTTP calls in
[The contract](#the-contract) below.

| | **Hub** — `TTRUCurtis/Texet-Twilio` | **Texet** — `wwbp/texet` (this repo) |
|---|---|---|
| Owns | The **participant** and the **study** | The **conversation** and the **model** |
| Knows | Phone numbers, enrollment, arms, cohorts, timezones, send-time preferences, surveys | Utterances, prompts, weekly summaries, token usage |
| Never sees | Model prompts, chat history, LLM credentials | Phone numbers, PII, survey responses, Twilio credentials |
| Talks to | Twilio, Qualtrics, AWARE, texet | Bedrock, OpenAI moderation, hub |
| EB app | `twilio-*` | `twilio-texet` / env `bot-prod` |

That split is the important thing to hold onto: **texet never learns who a participant is.**
It receives an opaque `participant_id` and returns text. All identity stays in the hub.

---

## Diagrams

The four views — layering, architecture, infrastructure, data flow — are in
[`architecture.tex`](architecture.tex), built to `architecture.pdf`:

```
tectonic docs/architecture.tex
```

This document carries what a picture cannot: the exact payloads, the ownership
split, and what happens when each part fails.

---

## The contract

Everything between the two services is these two calls. Both are authenticated with a
bearer token; neither is a shared database.

### 1. Hub → Texet — `POST /response`

Sent for **both** a participant's message and the hub's own daily opener. `is_initial`
is what separates them.

```jsonc
{
  "user_id": "<participant_id>",     // opaque to texet — never a phone number
  "input":   "<message text>",
  "mode":    "text",
  "metadata": {
    "source":          "sms",
    "twilio_sid":      "SM…",
    "is_initial":      false,         // true = hub's opener, store as-is, no model call
    "day_number":      12,            // selects the DailyPrompt
    "user_local_time": "2026-08-22T09:00:00-05:00"
  }
}
```

`user_local_time` is load-bearing well beyond logging: it is the **only** source of a
participant's timezone that texet has, and it drives day markers in chat history, the
`/engagement` day buckets, and which local week a summary covers. A participant whose
messages never carry it silently falls back to UTC everywhere.

Texet replies `202` with `user_utterance_id`, which the hub stores against its own
`Message` row. Nothing is generated yet.

### 2. Texet → Hub — `POST /api/messages/create`

```jsonc
{
  "participant_id":          "<participant_id>",
  "message":                 "<reply text>",
  "message_type":            "sent",
  "utterance_id":            "<bot utterance id>",
  "in_reply_to_utterance_id": "<user utterance id>"   // omitted for unprompted sends
}
```

The hub re-checks that the participant is still `ENROLLED`, splits the reply into SMS
segments with a delay between them, and sends each through Twilio.

Texet sends this **before** committing the utterance, so a rejected send rolls the reply
back rather than leaving a message marked delivered that never went out.

---

## Where things live

| Concern | Service | Notes |
|---|---|---|
| Phone numbers, PII | Hub | Never crosses to texet |
| Enrollment, arm, cohort | Hub | Gates every outbound message |
| Send-time preferences | Hub | `preferred_hour` per window, matched hourly against local time |
| Message templates by study day | Hub | Keyed `(arm, day_number, window)` |
| Twilio credentials + signature checks | Hub | |
| Qualtrics / AWARE integrations | Hub | Texet has no knowledge of these |
| SMS segmentation | Hub | Texet emits one string and does not know the 160-char limit |
| Conversation history | Texet | Windowed to the participant's current local week |
| System / daily / summarization prompts | Texet | DB-backed, editable in the console |
| Model choice and credentials | Texet | Bedrock Llama 4 Maverick |
| Moderation | Texet | OpenAI `omni-moderation-latest`, self-harm and sexual only |
| Weekly summaries | Texet | Hourly job, each participant's own local week |
| Token usage | Texet | Captured from Bedrock, exposed via `/engagement` |

---

## Failure modes worth knowing

| If this fails | What happens |
|---|---|
| Texet is down or slow | Hub marks the message `F_TIMEOUT` / `F_NETWORK` and returns 504/502 to Twilio. The participant's text is stored in the hub; **no reply is generated and none is retried.** |
| Hub is down when a reply is ready | Texet's send raises, the utterance is rolled back to `queued`, and the reply worker retries it up to `WORKER_MAX_ATTEMPTS`. |
| Participant un-enrolled mid-conversation | Hub returns 409 on `create`; texet treats it as a send failure and retries, then fails the utterance. |
| Bedrock or moderation fails | Reply worker records the error and retries; after the attempt limit the utterance is marked `failed` and surfaces in `/console/failures`. |
| `user_local_time` missing | Everything still works, on UTC. Day markers, engagement days, and week boundaries all shift for that participant. |

---

## Deployment

Both services deploy from `main` on a merge; there is no manual deploy step.

| | Hub | Texet |
|---|---|---|
| CI | on PR and push to `main` | on PR and push to `main` |
| CD | on successful CI against `main` | `.github/workflows/cd.yml` → Elastic Beanstalk |
| Migrations | Alembic, on deploy | Alembic, on deploy via the container entrypoint |
| Scheduler | APScheduler in-process | APScheduler in-process, guarded by a Postgres advisory lock so only one instance runs the job |
