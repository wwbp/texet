from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.models import Base


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_load_env_file(PROJECT_ROOT / ".env.db")


async def _ensure_test_db(database_url: str, reset: bool = False) -> None:
    url = make_url(database_url)
    if not url.database:
        raise RuntimeError("DATABASE_URL_TEST is missing a database name.")

    admin_url = url.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
    try:
        if reset:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                url.database,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}"')
            await conn.execute(f'CREATE DATABASE "{url.database}"')
            return

        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", url.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


async def _fetch_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = set(result.scalars().all())
    await engine.dispose()
    return tables


def _apply_migrations(database_url: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    os.environ["DATABASE_URL"] = database_url
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db() -> None:
    database_url = os.getenv("DATABASE_URL_TEST")
    if not database_url:
        raise RuntimeError("DATABASE_URL_TEST is not set.")

    asyncio.run(_ensure_test_db(database_url, reset=True))
    _apply_migrations(database_url)

    expected = {table.name for table in Base.metadata.sorted_tables}
    actual = asyncio.run(_fetch_tables(database_url))
    actual.discard("alembic_version")
    if expected != actual:
        raise RuntimeError(
            f"Migration mismatch. Expected tables {sorted(expected)}; "
            f"found {sorted(actual)}."
        )


@pytest.fixture()
async def async_session() -> AsyncSession:
    database_url = os.getenv("DATABASE_URL_TEST")
    if not database_url:
        raise RuntimeError("DATABASE_URL_TEST is not set.")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        table_list = ", ".join(
            f'"{table.name}"' for table in Base.metadata.sorted_tables
        )
        if table_list:
            await connection.execute(
                text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE")
            )

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session

    await engine.dispose()
