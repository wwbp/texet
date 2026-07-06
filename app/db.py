import os
from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import DEFAULT_TIMEZONE_NAME


def _pool_setting(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


@lru_cache
def get_engine() -> AsyncEngine:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        # DB_POOL_SIZE / DB_MAX_OVERFLOW: SQLAlchemy defaults (5 / 10) unless overridden.
        pool_size=_pool_setting("DB_POOL_SIZE", 5),
        max_overflow=_pool_setting("DB_MAX_OVERFLOW", 10),
        connect_args={"server_settings": {"timezone": DEFAULT_TIMEZONE_NAME}},
    )


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


async def ping_db() -> bool:
    engine = get_engine()
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        value = int(result.scalar_one())
        return value == 1
