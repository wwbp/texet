import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client_with_token(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("API_TOKEN", "test-token")
    return TestClient(app)


def test_chat_requires_auth(client_with_token: TestClient) -> None:
    client = client_with_token
    response = client.post("/chat", json={"user_id": "u1", "message": "hello"})
    assert response.status_code == 401


def test_chat_validates_payload(client_with_token: TestClient) -> None:
    client = client_with_token
    response = client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json={"user_id": "u1"},
    )
    assert response.status_code == 422

def test_chat_success(client_with_token: TestClient) -> None:
    client = client_with_token
    payload = {"user_id": "u1", "message": "hello"}
    response = client.post(
        "/chat",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    assert response.status_code == 200
    assert response.json() == {"user_id": "u1", "message": "hello", "status": "received"}
