# Texet API

## What this does

Texet is a small API service that:

- accepts inbound messages for a user,
- stores them in Postgres,
- generates a reply using OpenAI,
- sends the reply to your SMS webhook.

There is no end-user UI. You interact with it via HTTP or the admin dashboard.

## Quick start (Docker, no Python required)

Prereqs:

- Docker Desktop installed and running.
- A text editor to edit `.env` files.
- An OpenAI API key and an SMS webhook URL (optional for smoke tests).

Command examples below are `bash` (macOS/Linux). On Windows, use WSL or PowerShell equivalents.
This README uses `make` targets for consistency; see `Makefile` for what each target runs and
`docker-compose.yml` for the local services.

1) Create local env files:

   ```bash
   cp .env.db.example .env.db
   cp .env.api.example .env.api
   ```

2) Edit `.env.api` and set:
   - `API_TOKEN` to any random string you will use in requests.
   - `OPENAI_API_KEY` and `OPENAI_MODEL` (example: `gpt-4o-mini`).
   - `SMS_OUTBOUND_URL` to your SMS webhook endpoint (for testing, a request bin works).
   - If you change DB creds in `.env.db`, update `DATABASE_URL` and `DATABASE_URL_TEST`
     in `.env.api` to match.

3) Start the stack:

   ```bash
   make start
   ```

4) Apply migrations:

   ```bash
   make migrate
   ```

5) Verify in a browser:
   - `http://localhost:8000/health`
   - `http://localhost:8000/docs` (interactive API docs)

6) Send a message:
   - In `/docs`, use POST `/chat` and set the header `Authorization: Bearer <API_TOKEN>`.
   - The API returns `202 queued`. The reply is sent to `SMS_OUTBOUND_URL`.

To stop everything:

```bash
make stop
```

Note: `make stop` removes volumes, which wipes local DB data.

Want to visualize the local setup? Skim `Makefile` (command entry points) and
`docker-compose.yml` (services and ports). If these tools are new, the docs help:
<https://docs.docker.com/get-started/>, <https://docs.docker.com/compose/>, and
<https://makefiletutorial.com/>.

## Terms at a glance

- API: a URL-based interface you call with HTTP.
- Webhook: a URL you control that receives outbound replies.
- Bearer token: a shared secret sent in the `Authorization` header.
- Migration: a database change that creates or updates tables.
- Admin interface: the built-in dashboard at `/admin`.
- ORM: Python models mapped to database tables (SQLAlchemy).

## Using the API

- `GET /health` - service health.
- `GET /db/health` - database health.
- `POST /chat` - accepts `{ "user_id": "...", "message": "..." }`.

Example:

```bash
curl -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8000/chat \
  -d '{"user_id":"u1","message":"hello"}'
```

## Admin dashboard and exports

- Set `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `ADMIN_SECRET_KEY` in `.env.api`.
- Visit `http://localhost:8000/admin`.
- Admin views are read-only.
- Export data with Basic auth:
  - Paste these URLs into a browser after logging in, or use curl.
  - `GET /admin/export/utterances?format=csv|json&status=sent&since=2024-01-01T00:00:00-05:00`
  - `GET /admin/export/conversations?format=csv|json&status=open`
  - `GET /admin/export/speakers?format=csv|json`

## Configuration

Required for chat:

- `DATABASE_URL` - async SQLAlchemy URL for the app database.
- `API_TOKEN` - bearer token for `/chat`.
- `OPENAI_API_KEY` - OpenAI API key.
- `OPENAI_MODEL` - model name.
- `SMS_OUTBOUND_URL` - webhook for outbound replies.

Optional:

- `SMS_TIMEOUT_SECONDS` - outbound HTTP timeout in seconds (default `15`).
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY` - enable admin UI.
- `ADMIN_SESSION_TTL_SECONDS` - session TTL in seconds (default `28800`).
- `ADMIN_EXPORT_MAX_ROWS` - max rows per export (default `10000`).

Testing:

- `DATABASE_URL_TEST` - async SQLAlchemy URL for the test database (pytest).

Timezone:

- API and database sessions default to `EST` (UTC-05:00).

## Utterance status

- `received`: inbound user message stored.
- `queued`: outbound reply persisted, pending send.
- `sent`: outbound reply delivered to SMS webhook.
- `failed`: outbound reply failed; `error` captures the failure.

## Developer details (optional)

### Developer setup (uv)

Optional: local Python dev outside Docker. If you are just validating the project, use the
`make` commands above.

- Install uv: <https://docs.astral.sh/uv/getting-started/installation/>
- Create the local environment and lockfile:

  ```bash
  uv sync
  ```

- Run locally:

  ```bash
  uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  ```

### Dependencies

- Add a package:

  ```bash
  uv add <package>
  ```

- Upgrade a package:

  ```bash
  uv lock --upgrade-package <package>
  ```

### Migrations

- Migrations use Alembic and the `DATABASE_URL` from the running Compose stack.
- Migrations are how the database schema is created or updated.
- Define or update models in `app/models.py`, then generate a migration.
- Create a new migration:

  ```bash
  make migration name=add_speakers
  ```

- Apply migrations:

  ```bash
  make migrate
  ```

- If running locally (outside Docker), set `DATABASE_URL` before running Alembic.

### LLM integration

- Background task pipeline uses Kani with OpenAI for reply generation.
- Requires `OPENAI_API_KEY` and `OPENAI_MODEL`.
- Failures mark the reply utterance as `failed` with an error message.

### Tests

- Run the full test suite with coverage (Docker):

  ```bash
  make test
  ```

- Requires the DB running via `make start`.
- Uses the `texet_test` database and applies Alembic migrations before running.
- See `Makefile` for the exact commands.

### Smoke test

- Start the stack and apply migrations first:

  ```bash
  make start
  make migrate
  ```

- Run the end-to-end smoke test:

  ```bash
  make smoke
  ```

- If `SMS_OUTBOUND_URL` is empty, the smoke test expects SMS delivery to fail and will assert failed status counts instead of sent counts.

### Quality checks

- Run lint/format/typecheck/audit:

  ```bash
  make clean
  ```

- See `Makefile` for the exact commands.

### Make targets

- `make start` builds and starts the stack.
- `make stop` stops and removes the stack (including volumes).
- `make test` runs the full test suite with coverage (requires the DB running via `make start`).
- `make clean` runs linting, formatting, type checks, and vulnerability audit.
- `make migration name=...` creates a new Alembic revision (requires the DB running).
- `make migrate` applies Alembic migrations (requires the DB running).

## Notes

- Do not commit `.env` files or real API keys. Rotate any keys that have been shared.
- Model schema informed by ConvoKit: <https://convokit.cornell.edu/>
- Kani docs: <https://kani.readthedocs.io/>
