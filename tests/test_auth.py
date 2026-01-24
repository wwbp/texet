from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_session
from app.main import app


@pytest.mark.asyncio
async def test_auth_requires_configured_key(async_session: AsyncSession) -> None:
    async def _override_dependency() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/response",
            headers={"Authorization": "Bearer missing-key"},
            json={"user_id": "u1", "input": "hello"},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json()["detail"] == "API auth is not configured."
