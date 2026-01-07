# Working Log

## 2026-01-07
- Project initialized with baseline docs.
- Added FastAPI scaffold with Dockerfile and Docker Compose.
- Collected official uv documentation references for installation and project workflow.
- Migrated dependencies to `pyproject.toml` and updated Docker build to use uv.
- Pinned Python 3.12 for uv and project metadata.
- Built and started the API with Docker Compose; health check returned OK.
- Added `.gitignore` to avoid committing local artifacts.
- Added Postgres service to Docker Compose with env templates.
- Verified Postgres is accepting connections via `pg_isready`.
- Wired the API to Postgres via async SQLAlchemy + asyncpg and added a DB connection test.
- Standardized DB URLs to use the Compose service host for both main and test databases.
- Fixed the DB test to keep passwords intact and verified it passes in Compose.
