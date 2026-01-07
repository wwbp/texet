import os
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    return url


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(_get_database_url(), pool_pre_ping=True)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def ping_db() -> bool:
    engine = get_engine()
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        return result.scalar_one() == 1
