"""Reply worker: claims queued bot utterances from Postgres and generates replies.

Run as a standalone service (`python -m app.worker`). Any number of worker
processes can run concurrently — per-user ordering is enforced by the claim
query in app.queue, not by process-local state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import (
    get_worker_concurrency,
    get_worker_poll_interval_seconds,
    mock_external_apis,
)
from app.db import get_sessionmaker
from app.models.response import Conversation, Utterance
from app.queue import NOTIFY_CHANNEL, claim_next_reply, reclaim_stale
from app.response.service import _mark_bot_utterance_failed, _process_queued_reply

_logger = logging.getLogger(__name__)

_RECLAIM_INTERVAL_SECONDS = 30.0


def _asyncpg_dsn() -> str:
    """DATABASE_URL as a plain asyncpg DSN (drop the SQLAlchemy +asyncpg suffix)."""
    return os.getenv("DATABASE_URL", "").replace("+asyncpg", "", 1)


async def _listen_loop(notify_event: asyncio.Event, shutdown: asyncio.Event) -> None:
    """Keep a Postgres LISTEN connection open and wake workers on each NOTIFY.

    Reconnects on failure. If it can't listen, workers still make progress via
    the fallback poll — NOTIFY is an optimization, not a correctness dependency.
    """
    dsn = _asyncpg_dsn()
    if not dsn:
        _logger.warning("DATABASE_URL not set; NOTIFY listener disabled (polling only).")
        return
    while not shutdown.is_set():
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_a: notify_event.set())
            _logger.info("Listening for reply notifications on '%s'.", NOTIFY_CHANNEL)
            while not shutdown.is_set():
                if conn.is_closed():
                    raise ConnectionError("LISTEN connection closed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=10.0)
        except Exception:
            if not shutdown.is_set():
                _logger.exception("NOTIFY listener error; reconnecting shortly.")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=5.0)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()


async def process_one(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Claim and fully process one queued reply. Returns False if nothing was claimable."""
    async with sessionmaker() as session:
        bot_utterance = await claim_next_reply(session)
        if bot_utterance is None:
            return False
        user_utterance = (
            await session.get(Utterance, bot_utterance.reply_to_id)
            if bot_utterance.reply_to_id
            else None
        )
        conversation = await session.get(Conversation, bot_utterance.conversation_id)
    try:
        if not bot_utterance.reply_to_id:
            raise RuntimeError("Queued bot utterance missing reply_to_id.")
        if not user_utterance:
            raise RuntimeError("User utterance missing.")
        if not conversation:
            raise RuntimeError("Conversation missing.")
        await _process_queued_reply(
            sessionmaker, conversation.owner_speaker_id, user_utterance, bot_utterance
        )
    except Exception as exc:
        async with sessionmaker() as session:
            await _mark_bot_utterance_failed(session, bot_utterance.id, exc)
    return True


async def _worker_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    shutdown: asyncio.Event,
    notify_event: asyncio.Event,
    poll_interval: float,
) -> None:
    while not shutdown.is_set():
        try:
            processed = await process_one(sessionmaker)
        except Exception:
            _logger.exception("Worker iteration failed.")
            processed = False
        if not processed:
            # Idle: sleep until the API notifies of new work, or the fallback
            # interval elapses (covers missed notifications and reclaimed items).
            notify_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(notify_event.wait(), timeout=poll_interval)


async def _reclaim_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            async with sessionmaker() as session:
                requeued = await reclaim_stale(session)
            if requeued:
                _logger.warning("Reclaimed %d stale reply claims.", requeued)
        except Exception:
            _logger.exception("Reclaim pass failed.")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=_RECLAIM_INTERVAL_SECONDS)


async def run() -> None:
    concurrency = get_worker_concurrency()
    poll_interval = get_worker_poll_interval_seconds()
    sessionmaker = get_sessionmaker()

    shutdown = asyncio.Event()
    notify_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        shutdown.set()
        notify_event.set()  # wake idle worker loops so they observe shutdown

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    if mock_external_apis():
        _logger.warning(
            "MOCK_EXTERNAL_APIS is enabled — LLM, moderation, and SMS calls are faked. "
            "Load testing only; disable in production."
        )
    _logger.info("Reply worker starting with concurrency=%d.", concurrency)

    tasks = [
        asyncio.create_task(_worker_loop(sessionmaker, shutdown, notify_event, poll_interval))
        for _ in range(concurrency)
    ]
    tasks.append(asyncio.create_task(_reclaim_loop(sessionmaker, shutdown)))
    tasks.append(asyncio.create_task(_listen_loop(notify_event, shutdown)))
    await asyncio.gather(*tasks)
    _logger.info("Reply worker stopped.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
