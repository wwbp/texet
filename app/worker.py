"""Reply worker: claims queued bot utterances from Postgres and generates replies.

Run as a standalone service (`python -m app.worker`). Any number of worker
processes can run concurrently — per-user ordering is enforced by the claim
query in app.queue, not by process-local state.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import (
    get_worker_concurrency,
    get_worker_poll_interval_seconds,
    mock_external_apis,
)
from app.db import get_sessionmaker
from app.models.response import Conversation, Utterance
from app.queue import claim_next_reply, reclaim_stale
from app.response.service import _mark_bot_utterance_failed, _process_queued_reply

_logger = logging.getLogger(__name__)

_RECLAIM_INTERVAL_SECONDS = 30.0


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
    poll_interval: float,
) -> None:
    while not shutdown.is_set():
        try:
            processed = await process_one(sessionmaker)
        except Exception:
            _logger.exception("Worker iteration failed.")
            processed = False
        if not processed:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
            except TimeoutError:
                pass


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
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_RECLAIM_INTERVAL_SECONDS)
        except TimeoutError:
            pass


async def run() -> None:
    concurrency = get_worker_concurrency()
    poll_interval = get_worker_poll_interval_seconds()
    sessionmaker = get_sessionmaker()

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    if mock_external_apis():
        _logger.warning(
            "MOCK_EXTERNAL_APIS is enabled — LLM, moderation, and SMS calls are faked. "
            "Load testing only; disable in production."
        )
    _logger.info("Reply worker starting with concurrency=%d.", concurrency)

    tasks = [
        asyncio.create_task(_worker_loop(sessionmaker, shutdown, poll_interval))
        for _ in range(concurrency)
    ]
    tasks.append(asyncio.create_task(_reclaim_loop(sessionmaker, shutdown)))
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
