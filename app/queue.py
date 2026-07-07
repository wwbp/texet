"""Postgres-backed work queue over bot utterances with status='queued'.

Workers claim replies with FOR UPDATE SKIP LOCKED. A claim takes a user's
oldest queued reply only if that user has nothing in 'processing', which
preserves per-user ordering across any number of worker processes.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_PROCESSING,
    UTTERANCE_STATUS_QUEUED,
    get_worker_max_attempts,
    get_worker_reclaim_seconds,
)
from app.models.response import Utterance

_CLAIM_SQL = text(
    """
    WITH next AS (
        SELECT u.id
        FROM utterances u
        WHERE u.status = :queued
          AND NOT EXISTS (
              SELECT 1 FROM utterances p
              WHERE p.speaker_id = u.speaker_id AND p.status = :processing
          )
          AND u.id = (
              SELECT o.id FROM utterances o
              WHERE o.speaker_id = u.speaker_id AND o.status = :queued
              ORDER BY o.timestamp, o.id
              LIMIT 1
          )
        ORDER BY u.timestamp, u.id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE utterances
    SET status = :processing, claimed_at = now(), attempts = attempts + 1
    FROM next
    WHERE utterances.id = next.id
    RETURNING utterances.id
    """
)


async def claim_next_reply(session: AsyncSession) -> Utterance | None:
    """Claim the next processable queued reply, or None if nothing is claimable.

    The claim is committed immediately so it is visible to all other workers.
    """
    result = await session.execute(
        _CLAIM_SQL,
        {
            "queued": UTTERANCE_STATUS_QUEUED,
            "processing": UTTERANCE_STATUS_PROCESSING,
        },
    )
    claimed_id = result.scalar_one_or_none()
    await session.commit()
    if claimed_id is None:
        return None
    # populate_existing: the raw-SQL claim bypasses the ORM identity map.
    return await session.get(Utterance, claimed_id, populate_existing=True)


async def reclaim_stale(
    session: AsyncSession,
    *,
    reclaim_seconds: int | None = None,
    max_attempts: int | None = None,
) -> int:
    """Return expired 'processing' claims to the queue; fail replies out of attempts.

    Returns the number of replies re-queued.
    """
    reclaim_seconds = reclaim_seconds if reclaim_seconds is not None else get_worker_reclaim_seconds()
    max_attempts = max_attempts if max_attempts is not None else get_worker_max_attempts()
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=reclaim_seconds)

    await session.execute(
        update(Utterance)
        .where(
            Utterance.status == UTTERANCE_STATUS_PROCESSING,
            Utterance.claimed_at < cutoff,
            Utterance.attempts >= max_attempts,
        )
        .values(
            status=UTTERANCE_STATUS_FAILED,
            claimed_at=None,
            error=f"Reply abandoned after {max_attempts} stale claims.",
        )
    )
    requeued = await session.execute(
        update(Utterance)
        .where(
            Utterance.status == UTTERANCE_STATUS_PROCESSING,
            Utterance.claimed_at < cutoff,
        )
        .values(status=UTTERANCE_STATUS_QUEUED, claimed_at=None)
    )
    await session.commit()
    return int(requeued.rowcount or 0)


async def count_pending_replies(session: AsyncSession) -> int:
    """Count replies that are queued or in flight (backpressure signal)."""
    result = await session.execute(
        select(func.count())
        .select_from(Utterance)
        .where(
            Utterance.status.in_((UTTERANCE_STATUS_QUEUED, UTTERANCE_STATUS_PROCESSING))
        )
    )
    return int(result.scalar_one())
