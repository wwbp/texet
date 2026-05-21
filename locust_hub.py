from __future__ import annotations

import itertools
import logging
import os
import random
import string
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from locust import HttpUser, SequentialTaskSet, between, events, task

logger = logging.getLogger(__name__)

API_KEY = os.environ["HUB_API_KEY"]

created_participants: list[dict] = []

_phone_counter = itertools.count(start=1)
_participant_counter = itertools.count(start=1)


def unique_phone() -> str:
    n = next(_phone_counter)
    return f"555{n:07d}"


def unique_participant_id() -> str:
    n = next(_participant_counter)
    return f"LOCUST-{n:08d}"


def random_message_sid() -> str:
    return "SM" + "".join(random.choices(string.hexdigits[:16], k=32))


class ParticipantMessageFlow(SequentialTaskSet):
    participant: dict | None = None

    def on_start(self) -> None:
        self.headers = {"Authorization": f"Bearer {API_KEY}"}
        if not created_participants:
            logger.error("No seeded participants available")
            self.interrupt()
            return
        self.participant = random.choice(created_participants)

    @task
    def receive_inbound_message(self) -> None:
        if not self.participant:
            return
        time.sleep(random.uniform(5, 15))
        with self.client.post(
            "/api/messages/receive",
            json={
                "from_": f"+1{self.participant['phone']}",
                "body": f"Test message from {self.participant['participant_id']}",
                "messageSid": random_message_sid(),
            },
            headers=self.headers,
            name="/messages/receive [POST]",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 202):
                r.success()
            elif r.status_code == 404:
                r.failure(f"Participant not found: {self.participant['participant_id']}")
            else:
                r.failure(f"Unexpected: {r.status_code} — {r.text[:100]}")

    @task(1)
    def receive_unknown_phone(self) -> None:
        with self.client.post(
            "/api/messages/receive",
            json={
                "from_": "+10000000000",
                "body": "Message from unknown number",
                "messageSid": random_message_sid(),
            },
            headers=self.headers,
            name="/messages/receive [unknown phone]",
            catch_response=True,
        ) as r:
            if r.status_code == 404:
                r.success()
            elif r.status_code in (200, 202):
                r.failure("Expected 404 for unknown phone but got success")
            else:
                r.failure(f"Unexpected: {r.status_code} — {r.text[:100]}")


class MessagingUser(HttpUser):
    weight = 5
    wait_time = between(1, 3)
    tasks = [ParticipantMessageFlow]


async def _cleanup() -> None:
    database_url = os.getenv("DATABASE_URL_STAGING", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL_STAGING is not set")

    engine = create_async_engine(database_url, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as session:
        for table, column in [
            ("messages", "participant_id"),
            ("participant_preferences", "participant_id"),
            ("enrollments", "participant_id"),
            ("participants", "participant_id"),
        ]:
            result = await session.execute(
                text(f"DELETE FROM {table} WHERE {column} LIKE 'LOCUST-%'")
            )
            logger.info(f"{table}: deleted {result.rowcount} rows")
        await session.commit()
        logger.info("Cleanup complete.")

    await engine.dispose()


@events.test_start.add_listener
def on_test_start(environment, **kwargs) -> None:
    if os.getenv("TEST_MODE", "false").lower() != "true":
        logger.warning(
            "TEST_MODE is not enabled — load testing will send real SMS messages and incur charges!"
        )
    else:
        logger.info("TEST MODE enabled — no real SMS will be sent")

    import httpx

    NUM_SEED_PARTICIPANTS = 1_200
    headers = {"Authorization": f"Bearer {API_KEY}"}

    logger.info(f"Seeding {NUM_SEED_PARTICIPANTS} participants before test starts...")

    with httpx.Client(base_url=environment.host, timeout=30.0) as client:
        for _ in range(NUM_SEED_PARTICIPANTS):
            payload = {
                "participant_id": unique_participant_id(),
                "email_id": "test@test.com",
                "phone": unique_phone(),
            }
            r = client.post("/api/participants", json=payload, headers=headers)
            if r.status_code in (200, 201):
                created_participants.append(r.json())
            else:
                logger.warning(
                    f"Failed to seed {payload['participant_id']}: {r.status_code} — {r.text[:100]}"
                )

    logger.info(
        f"Seeding complete — {len(created_participants)} participants ready. Starting load test..."
    )


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    import asyncio

    stats = environment.stats.total
    logger.info(
        f"Test complete | "
        f"requests={stats.num_requests} "
        f"failures={stats.num_failures} "
        f"p95={stats.get_response_time_percentile(0.95):.0f}ms"
    )
    logger.info(f"Participants seeded this run: {len(created_participants)}")
    logger.info("Starting cleanup...")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_cleanup(), loop)
            future.result(timeout=30)
        else:
            loop.run_until_complete(_cleanup())
    except Exception as e:
        logger.error(f"Cleanup failed — run manually: {e}")


@events.request.add_listener
def on_request(
    request_type,
    name,
    response_time,
    response_length,
    response,
    exception,
    **kwargs,
) -> None:
    if response_time > 2000:
        logger.warning(f"SLOW: {request_type} {name} — {response_time:.0f}ms")
    if response is not None and response.status_code >= 500:
        logger.error(
            f"SERVER ERROR: {request_type} {name} "
            f"status={response.status_code} body={response.text[:200]}"
        )
