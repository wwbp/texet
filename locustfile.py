import os
import random
import uuid

from locust import HttpUser, between, task

API_KEY = os.environ["TEXET_API_KEY"]

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
    # Short wait: /response is async (202 queued), not a blocking LLM call.
    # Adjust down to 0.1 for maximum DB stress once baseline is established.
    wait_time = between(0.5, 2)

    @task(10)
    def send_message(self) -> None:
        with self.client.post(
            "/response",
            json={
                "user_id": uuid.uuid4().hex,
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
