import datetime
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.auth import require_auth, require_export
from app.config import (
    DEFAULT_TIMEZONE,
    admin_enabled,
    get_admin_secret_key,
    get_admin_session_ttl_seconds,
    mock_external_apis,
    scheduler_enabled,
)
from app.console import console_router, init_console
from app.console.exports import EXPORT_VERSION, _ensure_tz, run_logged_export
from app.db import get_async_session, ping_db
from app.engagement.schemas import EngagementRow
from app.engagement.service import compute_engagement
from app.response import process_response
from app.response.schemas import ResponseQueuedResponse, ResponseRequest
from app.scheduler import start_scheduler, stop_scheduler


class _ForceHTTPSMiddleware:
    """Force https scheme so sqladmin generates https:// asset URLs when behind a TLS proxy."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope.get("type") in ("http", "websocket"):
            scope = {**scope, "scheme": "https"}
        await self.app(scope, receive, send)  # type: ignore[operator]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if mock_external_apis():
        logging.getLogger(__name__).warning(
            "MOCK_EXTERNAL_APIS is enabled — LLM, moderation, and SMS calls are faked. "
            "Load testing only; disable in production."
        )
    init_console(app)
    if scheduler_enabled():
        start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title="Texet API",
    version="0.1.0",
    description="Base API scaffold for Texet.",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

OPENAPI_DOCS_PATH_WHITELIST = {"/response", "/health", "/db/health"}


def openapi_schema_for_docs() -> dict[str, object]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    paths = schema.get("paths", {})
    schema["paths"] = {
        path: value for path, value in paths.items() if path in OPENAPI_DOCS_PATH_WHITELIST
    }
    app.openapi_schema = schema
    return schema


app.openapi = openapi_schema_for_docs  # type: ignore[method-assign]

if os.getenv("FORCE_HTTPS") == "true":
    app.add_middleware(_ForceHTTPSMiddleware)

if admin_enabled():
    admin_secret = get_admin_secret_key()
    if admin_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=admin_secret,
            max_age=get_admin_session_ttl_seconds(),
            same_site="lax",
        )

app.include_router(console_router)


@app.post(
    "/response",
    response_model=ResponseQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_auth)],
)
async def response(
    payload: ResponseRequest,
    session: AsyncSession = Depends(get_async_session),
) -> ResponseQueuedResponse:
    return await process_response(session, payload)


@app.get("/exports/convokit", response_class=FileResponse)
async def export_convokit(
    background_tasks: BackgroundTasks,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
    include_generation_snapshot: bool = False,
    api_key_id: str = Depends(require_export),
    session: AsyncSession = Depends(get_async_session),
) -> FileResponse:
    """The console's ConvoKit export, for scripts. Needs an export-scope key.

    Naive datetimes are read in the console's default timezone.
    """
    export_id, artifact = await run_logged_export(
        session,
        since=_ensure_tz(since) if since else None,
        until=_ensure_tz(until) if until else None,
        include_generation_snapshot=include_generation_snapshot,
        record_meta={"source": "api", "api_key_id": api_key_id},
    )
    background_tasks.add_task(shutil.rmtree, artifact.temp_dir, ignore_errors=True)
    return FileResponse(
        artifact.zip_path,
        media_type="application/zip",
        filename=artifact.zip_path.name,
        headers={
            "X-Texet-Export-Sha256": artifact.sha256,
            "X-Texet-Export-Id": export_id,
            "X-Texet-Export-Version": str(EXPORT_VERSION),
            "Cache-Control": "no-store",
        },
    )


@app.get(
    "/engagement",
    response_model=list[EngagementRow],
    dependencies=[Depends(require_auth)],
)
async def engagement(
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    session: AsyncSession = Depends(get_async_session),
) -> list[EngagementRow]:
    """Engagement per participant per pinged calendar day.

    A day appears only if the chatbot pinged the participant on it, and counts
    as engaged when they sent at least one message that same day. Days are the
    participant's local days, so an evening reply is not pushed onto tomorrow.
    """
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=422,
            detail="start must not be after end.",
        )
    rows = await compute_engagement(session, start=start, end=end)
    return [EngagementRow(**row.as_dict()) for row in rows]


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
