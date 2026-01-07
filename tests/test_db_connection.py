import os

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_db_connection() -> None:
    database_url = os.getenv("DATABASE_URL_TEST")
    if not database_url:
        raise RuntimeError("DATABASE_URL_TEST is not set.")
    url = make_url(database_url)
    admin_url = url.set(drivername="postgresql", database="postgres")

    conn = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", url.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()

    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    await engine.dispose()
