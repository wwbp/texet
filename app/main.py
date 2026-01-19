import datetime
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.admin import init_admin
from app.admin import router as admin_router
from app.auth import require_auth
from app.chat import process_chat
from app.config import (
    DEFAULT_TIMEZONE,
    admin_enabled,
    get_admin_secret_key,
    get_admin_session_ttl_seconds,
)
from app.db import get_async_session, ping_db
from app.schemas import ChatQueuedResponse, ChatRequest


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_admin(app)
    yield


app = FastAPI(
    title="Texet API",
    version="0.1.0",
    description="Base API scaffold for Texet.",
    lifespan=lifespan,
)

if admin_enabled():
    admin_secret = get_admin_secret_key()
    if admin_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=admin_secret,
            max_age=get_admin_session_ttl_seconds(),
            same_site="lax",
        )

app.include_router(admin_router)


@app.post(
    "/chat",
    response_model=ChatQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_auth)],
)
async def chat(
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_async_session),
) -> ChatQueuedResponse:
    return await process_chat(session, payload, background_tasks)


@app.get("/", response_class=JSONResponse)
def root() -> dict[str, str]:
    return {
        "message": "Texet API is running.",
        "timestamp": datetime.datetime.now(DEFAULT_TIMEZONE).isoformat(),
    }


@app.get("/health", response_class=JSONResponse)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/db/health", response_class=JSONResponse)
async def db_health() -> dict[str, str]:
    try:
        ok = await ping_db()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database not reachable.") from exc

    return {"status": "ok" if ok else "error"}
