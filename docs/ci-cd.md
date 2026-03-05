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

## Notes

- If test setup changes, update CI env vars and test fixtures together.
