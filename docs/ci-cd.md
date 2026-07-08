# CI/CD Quick Guide

## What runs on GitHub

- CI workflow file: `.github/workflows/ci.yml`
- CD workflow file: `.github/workflows/cd.yml`
Triggers:
- `pull_request`: run CI.
- `push` to `main`: run CI.
- CD is triggered by successful CI for `push` to `main` (`workflow_run`).

## CI (tests)

- Job: `ci`
- Runner: `ubuntu-latest`
- Database: Postgres service container (`postgres:16-alpine`)
- Test command: `uv run pytest --cov`
DB URLs are provided via:
- `DATABASE_URL`
- `DATABASE_URL_TEST`

Why Postgres is required:

- Test fixtures create/reset `texet_test` and apply Alembic migrations before tests.
- Many tests assert database behavior directly.

No-internet test guard:

- `tests/conftest.py` blocks external hostname resolution during tests.
- Only loopback and configured DB hosts are allowed.
- If a test accidentally calls a real external API, it fails fast.

## CD (Elastic Beanstalk)

- Job: `deploy_backend`
- Runs only on `push` to `main` and only after CI succeeds.
- Uses GitHub Environment: `production` (for protection rules/approvals).
- Auth: GitHub OIDC with `aws-actions/configure-aws-credentials@v4`.
- Deploy: `aws-actions/aws-elasticbeanstalk-deploy@v1.0.0`.

Required GitHub secrets:

- `AWS_ACCOUNT_ID`
- `AWS_ROLE_NAME`
- `AWS_BACKEND_APPLICATION_NAME`
- `AWS_PROD_BACKEND_ENVIRONMENT_NAME`

## Reply worker on Elastic Beanstalk

Reply generation runs in a separate process (`python -m app.worker`) that claims
queued utterances from Postgres (see `app/worker.py`, `app/queue.py`). bot-prod
is a **single-container** EB environment, so that worker runs *inside the same
container* as the web server: `RUN_WORKER_INLINE=true`
(`.ebextensions/00-app-env.config`) makes `scripts/docker-entrypoint.sh` launch
the worker alongside uvicorn. If either process exits, the container exits and EB
restarts it. (ECS and local compose instead run a dedicated worker service and
leave `RUN_WORKER_INLINE` unset — see `infra/terraform/` and
`docker-compose.yml`.)

Deploy notes for the queue rollout:

- **The worker must be running or replies never send.** The api only enqueues;
  `RUN_WORKER_INLINE` is what processes the queue on EB. Do not remove it.
- **Migrations run on deploy** via the entrypoint (`alembic upgrade head`). The
  queue migrations add a `processing` status, `claimed_at`/`attempts` columns,
  and indexes on `utterances`. `CREATE INDEX` (non-concurrent, inside the
  migration transaction) briefly locks writes on `utterances` while it builds —
  deploy during low traffic if that table is large.

## Notes

- If test setup changes, update CI env vars and test fixtures together.
