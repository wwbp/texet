# Texet — Data Flow Diagram

> **Audience:** Engineering team / new contributors
> **Last updated:** 2026-05-29
> **Scope:** Core SMS chatbot backend — inbound message handling, background processing, scheduled jobs, admin

---

## Legend

| Symbol | Meaning |
|--------|---------|
| `║` | Infrastructure boundary (vertical parallel line = a distinct system/service) |
| `┌─ Box ─┐` or `[ Box ]` | Processing stage (code executes here) |
| `──►` | Data flows in this direction |
| `◄──` | Data returns / response |
| `╠──X──►` | Data crosses from FastAPI into the named infra column |
| `═══ SECTION ═══` | Top-level lifecycle phase |
| `──` dashed line | Conditional branch |

---

## Quick Term Reference

| Term | What it is |
|------|-----------|
| **SMS Hub** | External SMS platform that POSTs inbound messages to `/response` and receives outbound delivery via a webhook URL (`SMS_OUTBOUND_URL`) |
| **Bearer token** | API key sent in `Authorization: Bearer <token>` header; stored as a SHA-256 hash in the DB, never in plaintext |
| **Speaker** | A participant row in the DB — one per real user, plus a synthetic `bot:<user_id>` speaker per user |
| **Conversation** | A container for an ordered set of Utterances; keyed by `owner_speaker_id` + `status=open`; at most one open conversation per user, created on their first message and reused for all subsequent ones |
| **Utterance** | A single message (user or bot) within a Conversation; has `status`, `text`, `speaker_id`, `reply_to_id`, `meta` (JSON), `timestamp` |
| **Utterance status** | `queued` → bot reply placeholder; `received` → user message stored; `sent` → delivered to SMS Hub; `moderated` → blocked; `failed` → delivery error |
| **get-or-create** | SELECT first; if not found, INSERT inside a savepoint; if race/IntegrityError, SELECT again — existing row is never modified |
| **is_initial** | Flag in request metadata — marks a pre-written bot opener injected by the hub to seed a conversation, bypasses the full pipeline |
| **day_number** | Integer in request metadata that selects a `DailyPrompt` record (topic/activity for that study day) |
| **DailyPrompt** | Admin-configured per-day content injected into the system prompt |
| **SystemPrompt** | Admin-configured base instruction prompt; latest version wins |
| **WeeklySummary** | LLM-generated 3–5 sentence summary of the prior week's conversation; injected as context the following week |
| **omni-moderation** | OpenAI `omni-moderation-latest` API — returns per-category scores; applied to both user input and bot output |
| **Kani** | Python LLM orchestration library wrapping OpenAI and Bedrock engines; manages chat history + system prompt |
| **asyncio.Lock** | In-process per-user mutex that prevents concurrent background processing for the same user (single-replica only) |
| **APScheduler** | In-process async job scheduler; fires the weekly summary job every Sunday midnight UTC |
| **ConvoKit** | NLP toolkit format used for research data export (synchronous, can block on large datasets) |

---

## Diagram

```
                                 DATA FLOW — TEXET SMS CHATBOT
═══════════════════════════════════════════════════════════════════════════════════════════════════

  SMS Hub (ext)         ║       FastAPI App            ║  PostgreSQL  ║  OpenAI/Bedrock  ║  SMTP
                        ║                              ║              ║                  ║
════════════════════════╬══════════ INBOUND (sync · returns 202 immediately) ═══════════╬══════════
                        ║                              ║              ║                  ║
  POST /response ───────╬──► ┌──────────────────────┐ ║              ║                  ║
  user_id, input,       ║    │ Auth Check           │ ╠──SELECT──────►                  ║
  metadata, mode        ║    │ SHA-256 Bearer token  │ ◄──active key──╣                  ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
                        ║    ┌──────────▼───────────┐ ║              ║                  ║
                        ║    │ process_response()   │ ║              ║                  ║
                        ║    │                      │ ║              ║                  ║
                        ║    │ if is_initial:        │ ╠──INSERT──────►  (bot utterance  ║
                        ║    │   persist bot utt    │ ║   status=SENT ║   with raw text) ║
                        ║    │   → 202 "recorded"   │ ║              ║                  ║
                        ║    │                      │ ║              ║                  ║
                        ║    │ else:                │ ║              ║                  ║
                        ║    │   get-or-create      ├─╬──SELECT/─────►  Speaker         ║
                        ║    │     Speaker          │ ║   INSERT     ║                  ║
                        ║    │   get-or-create      ├─╬──SELECT/─────►  Conversation    ║
                        ║    │     Conversation     │ ║   INSERT     ║                  ║
                        ║    │   INSERT user utt    ├─╬──RECEIVED────►                  ║
                        ║    │   INSERT bot utt     ├─╬──QUEUED/null─►                  ║
                        ║    │   schedule bg task   │ ║              ║                  ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
  ◄── 202 Accepted ─────╬───────────────┘             ║              ║                  ║
                        ║                              ║              ║                  ║
════════════════════════╬═ BACKGROUND  _drain_user_queue · per-user asyncio.Lock ═══════╬══════════
                        ║   (loop: process each QUEUED bot utterance in order)          ║
                        ║                              ║              ║                  ║
                        ║    ┌─────────────────────┐  ║              ║                  ║
                        ║    │ Moderate User Input │  ╠──────────────╬──►omni-moderation║
                        ║    │ send utterance text │  ◄──────────────╬── score/category ║
                        ║    └──────────┬──────────┘  ║              ║                  ║
                        ║               │             ║              ║                  ║
                        ║       ┌───────▼── if blocked: ──────────────────────────────┐ ║
                        ║       │ Handle Flagged User Msg                             │ ║
                        ║       │  mark user utt MODERATED  ──────────► UPDATE        │ ║
                        ║       │  build recent chat history ─────────► SELECT        │ ║
                        ║       │  send moderation alert email ─────────────────────────╬──► email
                        ║       │  send crisis notice SMS to user                     │ ║
                        ║  ◄────┤  "988 crisis line" message                          │ ║
                        ║       │  mark bot utt MODERATED   ──────────► UPDATE        │ ║
                        ║       └────────────────── (loop next) ──────────────────────┘ ║
                        ║               │ else (not blocked):          ║                ║
                        ║               │                              ║                ║
                        ║    ┌──────────▼───────────┐                 ║                ║
                        ║    │ Compose System Prompt│                 ║                ║
                        ║    │  fetch system_prompt ├─╬──SELECT───────►                ║
                        ║    │  fetch daily_prompt  ├─╬──SELECT───────►  (by day_num)  ║
                        ║    │  fetch prev week     ├─╬──SELECT───────►  WeeklySummary ║
                        ║    │  summary             │ ║              ║                  ║
                        ║    │  compose:            │ ║              ║                  ║
                        ║    │   base_prompt        │ ║              ║                  ║
                        ║    │   + opening_message  │ ║              ║                  ║
                        ║    │   + daily_content    │ ║              ║                  ║
                        ║    │   + weekly_summary   │ ║              ║                  ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
                        ║    ┌──────────▼───────────┐ ║              ║                  ║
                        ║    │ Build Chat History   │ ║              ║                  ║
                        ║    │  SELECT utterances   ├─╬──SELECT───────► since week start║
                        ║    │  skip: moderated,    │ ║              ║                  ║
                        ║    │         hub_initial, │ ║              ║                  ║
                        ║    │         empty text   │ ║              ║                  ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
                        ║    ┌──────────▼───────────┐ ║              ║                  ║
                        ║    │ LLM Generate (Kani)  │ ╠──────────────╬──►gpt-4o-mini or ║
                        ║    │  system_prompt       │ ║              ║   AWS Bedrock    ║
                        ║    │  chat_history        │ ◄──────────────╬── reply text     ║
                        ║    │  + user_utterance    │ ║              ║                  ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
                        ║    ┌──────────▼───────────┐ ║              ║                  ║
                        ║    │ Moderate Bot Output  │ ╠──────────────╬──►omni-moderation║
                        ║    │  send reply text     │ ◄──────────────╬── score/category ║
                        ║    │                      │ ║              ║                  ║
                        ║    │  if blocked:         │ ║              ║                  ║
                        ║    │    store raw text    ├─╬──UPDATE───────►                 ║
                        ║    │    deliver notice    │ ║              ║                  ║
                        ║    │    mark MODERATED    ├─╬──UPDATE───────►                 ║
                        ║    │  else:               │ ║              ║                  ║
                        ║    │    mark SENT         ├─╬──UPDATE───────►                 ║
                        ║    └──────────┬───────────┘ ║              ║                  ║
                        ║               │             ║              ║                  ║
  ◄── bot reply SMS ────╬───────────────┘             ║              ║                  ║
      (or moderation    ║   POST to SMS_OUTBOUND_URL  ║              ║                  ║
       notice)          ║                              ║              ║                  ║
                        ║                              ║              ║                  ║
════════════════════════╬══ SCHEDULED · APScheduler · every Sunday midnight UTC ════════╬══════════
                        ║                              ║              ║                  ║
                        ║    ┌─────────────────────┐  ║              ║                  ║
                        ║    │ run_weekly_summaries│  ║              ║                  ║
                        ║    │  find users active  ├──╬──SELECT───────► distinct         ║
                        ║    │  last 7 days        │  ║   speaker_ids║                  ║
                        ║    │  for each user:     │  ║              ║                  ║
                        ║    │    fetch utterances ├──╬──SELECT───────►                 ║
                        ║    │    build transcript │  ║              ║                  ║
                        ║    │    LLM summarize    ├──╬──────────────╬──► gpt-4o-mini   ║
                        ║    │    (3-5 sentences)  ◄──╬──────────────╬── summary text   ║
                        ║    │    write summary    ├──╬──INSERT/UPDATE► WeeklySummary   ║
                        ║    └─────────────────────┘  ║              ║                  ║
                        ║                              ║              ║                  ║
════════════════════════╬══ ADMIN (session-auth · sqladmin) ═════════╬══════════════════╬══════════
                        ║                              ║              ║                  ║
                        ║    /console/admin            ╠──SELECT *────►  speakers,       ║
                        ║    conversations, utterances,║              ║  conversations,  ║
                        ║    speakers, API keys,       ║              ║  utterances,     ║
                        ║    system/daily prompts      ║              ║  api_keys, etc.  ║
                        ║                              ║              ║                  ║
                        ║    /console/exports          ╠──SELECT all──►  (sync, blocks   ║
                        ║    ConvoKit JSON export      ║   utterances ║   on large data) ║
                        ║                              ║              ║                  ║
═══════════════════════════════════════════════════════════════════════════════════════════════════
```

---

## Key Design Notes

| # | Note |
|---|------|
| 1 | **Immediate 202** — the HTTP response returns before any LLM or SMS work happens; all heavy lifting is background |
| 2 | **Per-user asyncio.Lock** — serializes replies per user so messages are processed in order; breaks under multiple server replicas |
| 3 | **Two moderation passes** — user input (gate: blocked → crisis notice + alert email) then bot output (filter: blocked → store raw, deliver sanitized notice) |
| 4 | **Dynamic system prompt** — assembled fresh per request from 4 sources: base prompt, opening message, daily content (by `day_number`), prior week's LLM summary |
| 5 | **Chat history is weekly-windowed** — only utterances since the current week's Sunday (UTC) are sent to the LLM; prevents unbounded context growth |
| 6 | **Weekly summary closes the loop** — scheduled job generates a summary that becomes injected context the following week |
| 7 | **Initial message shortcut** — `is_initial: true` bypasses the full pipeline; just persists the hub's pre-written opener as `SENT` |
