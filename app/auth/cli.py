from __future__ import annotations

import argparse
import asyncio

from app.auth.api_keys import create_api_key
from app.db import get_sessionmaker


async def _create_key(name: str | None) -> str:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        return await create_api_key(session, name=name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new API key.")
    parser.add_argument("--name", help="Optional key name", default=None)
    args = parser.parse_args()

    name = args.name.strip() if args.name else None
    key = asyncio.run(_create_key(name))
    print(key)


if __name__ == "__main__":
    main()
