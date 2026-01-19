import datetime

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import DEFAULT_TIMEZONE
from app.db import get_async_session, ping_db
from app.schemas import ChatQueuedResponse, ChatRequest
from app.services.chat import process_chat

app = FastAPI(
    title="Texet API",
    version="0.1.0",
    description="Base API scaffold for Texet.",
)


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
