import itertools
import logging
import os
import random

from locust import HttpUser, constant_throughput, events, task

logger = logging.getLogger(__name__)

API_KEY = os.environ["TEXET_API_KEY"]

# Each simulated user issues TEXET_RPS_PER_USER requests/second (all tasks combined).
# Target throughput = users × rate: 500 users × 0.2 = 100 rps.
RPS_PER_USER = float(os.getenv("TEXET_RPS_PER_USER", "0.2"))

# Stable per-user IDs so repeated messages land in the same conversation and
# exercise the per-user queue lock / drain path (a fresh uuid per request would
# skip that entirely). Prefixed for post-run cleanup (speakers.id is the user_id):
#   DELETE FROM utterances WHERE conversation_id IN
#     (SELECT id FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%');
#   DELETE FROM conversations WHERE owner_speaker_id LIKE 'loadtest-%';
#   DELETE FROM speakers WHERE id LIKE 'loadtest-%' OR id LIKE 'bot:loadtest-%';
_user_counter = itertools.count(start=1)

INPUTS = [
    "Hello, how are you today?",
    "What can you help me with?",
    "Tell me something interesting.",
    "Can you recommend a good book?",
    "What are you capable of?",
    "I need some advice.",
    "How does this work?",
    "Tell me a joke.",
    "What should I have for dinner?",
    "Help me brainstorm ideas.",
    "What do you think about that?",
    "I'm feeling stressed today.",
    "Give me a fun fact.",
    "What would you recommend?",
    "Can we talk?",
    "Explain that to me simply.",
    "What's on your mind?",
    "I'm not sure what to do.",
    "That's interesting, tell me more.",
    "What do you suggest?",
]


class TexetUser(HttpUser):
    # constant_throughput paces each user to a fixed request rate regardless of
    # response time, so total rps stays at users × RPS_PER_USER as load grows.
    wait_time = constant_throughput(RPS_PER_USER)

    def on_start(self) -> None:
        # PID keeps IDs unique when locust runs with --processes.
        self.user_id = f"loadtest-{os.getpid()}-{next(_user_counter):06d}"

    @task(10)
    def send_message(self) -> None:
        with self.client.post(
            "/response",
            json={
                "user_id": self.user_id,
                "input": random.choice(INPUTS),
                "mode": "text",
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
            catch_response=True,
            timeout=10,
        ) as r:
            if r.status_code == 202:
                r.success()
            elif r.status_code == 401:
                r.failure("401 Unauthorized — check TEXET_API_KEY")
            else:
                r.failure(f"{r.status_code}: {r.text[:120]}")

    @task(1)
    def health(self) -> None:
        self.client.get("/health")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    stats = environment.stats.total
    logger.info(
        f"Test complete | requests={stats.num_requests} "
        f"failures={stats.num_failures} "
        f"rps={stats.total_rps:.1f} "
        f"p50={stats.get_response_time_percentile(0.5):.0f}ms "
        f"p95={stats.get_response_time_percentile(0.95):.0f}ms "
        f"p99={stats.get_response_time_percentile(0.99):.0f}ms"
    )


@events.request.add_listener
def on_request(request_type, name, response_time, response, **kwargs) -> None:
    if response_time > 2000:
        logger.warning(f"SLOW: {request_type} {name} — {response_time:.0f}ms")
    if response is not None and getattr(response, "status_code", 0) >= 500:
        logger.error(
            f"SERVER ERROR: {request_type} {name} "
            f"status={response.status_code} body={response.text[:200]}"
        )
