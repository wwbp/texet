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

1) Create local env files:
   - `cp .env.db.example .env.db`
   - `cp .env.api.example .env.api`

2) Edit `.env.api` and set:
   - `API_TOKEN` to any random string you will use in requests.
   - `OPENAI_API_KEY` and `OPENAI_MODEL` (example: `gpt-4o-mini`).
   - `SMS_OUTBOUND_URL` to your SMS webhook endpoint (for testing, a request bin works).

3) Start the stack:
   - `docker compose up --build -d`
   - or `make start`

4) Apply migrations:
   - `make migrate`

5) Verify in a browser:
   - `http://localhost:8000/health`
   - `http://localhost:8000/docs` (interactive API docs)

6) Send a message:
   - In `/docs`, use POST `/chat` and set the header `Authorization: Bearer <API_TOKEN>`.
   - The API returns `202 queued`. The reply is sent to `SMS_OUTBOUND_URL`.

To stop everything:

- `docker compose down -v`
- or `make stop`

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
- Export data with Basic auth:
  - `GET /admin/export/utterances?format=csv|json&status=sent&since=2024-01-01T00:00:00-05:00`
  - `GET /admin/export/conversations?format=csv|json&status=open`
  - `GET /admin/export/speakers?format=csv|json`

## Configuration

Required for chat:

- `API_TOKEN` - bearer token for `/chat`.
- `OPENAI_API_KEY` - OpenAI API key.
- `OPENAI_MODEL` - model name.
- `SMS_OUTBOUND_URL` - webhook for outbound replies.

Optional:

- `SMS_TIMEOUT_SECONDS` - outbound HTTP timeout in seconds (default `15`).
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY` - enable admin UI.
- `ADMIN_SESSION_TTL_SECONDS` - session TTL in seconds (default `28800`).
- `ADMIN_EXPORT_MAX_ROWS` - max rows per export (default `10000`).

Timezone:

- API and database sessions default to `EST` (UTC-05:00).

## Utterance status

- `received`: inbound user message stored.
- `queued`: outbound reply persisted, pending send.
- `sent`: outbound reply delivered to SMS webhook.
- `failed`: outbound reply failed; `error` captures the failure.

## Developer setup (uv)

- Install uv: <https://docs.astral.sh/uv/getting-started/installation/>
- Create the local environment and lockfile:
  - `uv sync`
- Run locally:
  - `uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

### Dependencies

- Add a package:
  - `uv add <package>`
- Upgrade a package:
  - `uv lock --upgrade-package <package>`

## Migrations

- Migrations use Alembic and the `DATABASE_URL` from the running Compose stack.
- Define or update models in `app/models.py`, then generate a migration.
- Create a new migration:
  - `make migration name=add_speakers`
- Apply migrations:
  - `make migrate`
- If running locally (outside Docker), set `DATABASE_URL` before running Alembic.

## LLM integration

- Background task pipeline uses Kani with OpenAI for reply generation.
- Requires `OPENAI_API_KEY` and `OPENAI_MODEL`.
- Failures mark the reply utterance as `failed` with an error message.

## Tests

- Ensure the DB is running:
  - `docker compose up -d db`
- Run the connection test inside the Compose network (uses `DATABASE_URL_TEST` pointing at `db`):
  - `docker compose run --rm api uv run pytest tests/test_db_connection.py`
- The test database is `texet_test` inside the same Postgres container.
- Tests apply Alembic migrations to `texet_test` before running.
- Run chat endpoint tests:
  - `uv run pytest tests/test_chat_endpoint.py`
- Run all tests with coverage (local):
  - `uv run pytest --cov`
- Run all tests with coverage (Docker):
  - `docker compose run --rm api uv run pytest --cov`

## Smoke test

- Start the stack and apply migrations first:
  - `make start`
  - `make migrate`
- Run the end-to-end smoke test:
  - `make smoke`
- If `SMS_OUTBOUND_URL` is empty, the smoke test expects SMS delivery to fail and will assert failed status counts instead of sent counts.

## Quality checks

- Lint:
  - `uv run ruff check .`
- Format:
  - `uv run ruff format .`
- Type check:
  - `uv run mypy`
- Vulnerability audit:
  - `uv run pip-audit`
- Combined:
  - `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pip-audit`

## Make targets

- `make start` builds and starts the stack.
- `make stop` stops and removes the stack (including volumes).
- `make test` runs the full test suite with coverage (requires the DB running via `make start` or `docker compose up -d db`).
- `make clean` runs linting, formatting, type checks, and vulnerability audit.
- `make migration name=...` creates a new Alembic revision (requires the DB running).
- `make migrate` applies Alembic migrations (requires the DB running).

## Notes

- Do not commit `.env` files or real API keys. Rotate any keys that have been shared.
- Model schema informed by ConvoKit: <https://convokit.cornell.edu/>
- Kani docs: <https://kani.readthedocs.io/>
