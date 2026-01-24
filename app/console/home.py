from fastapi import Depends
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request

from app.config import CONSOLE_PREFIX
from app.console.core import console_router, require_admin


@console_router.get("", response_class=HTMLResponse)
async def console_root(_: None = Depends(require_admin)) -> HTMLResponse:
    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console</title>
      </head>
      <body>
        <h1>Texet Console</h1>
        <ul>
          <li><a href="{CONSOLE_PREFIX}/admin">Admin</a></li>
          <li><a href="{CONSOLE_PREFIX}/api-keys">API Keys</a></li>
          <li><a href="{CONSOLE_PREFIX}/exports">Exports</a></li>
          <li><a href="{CONSOLE_PREFIX}/docs">API Docs</a></li>
        </ul>
      </body>
    </html>
    """
    return HTMLResponse(html.strip())


@console_router.get("/docs", response_class=HTMLResponse)
async def console_docs(_: None = Depends(require_admin)) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=f"{CONSOLE_PREFIX}/openapi.json",
        title="Texet API Docs",
        oauth2_redirect_url=f"{CONSOLE_PREFIX}/docs/oauth2-redirect",
    )


@console_router.get("/docs/oauth2-redirect", response_class=HTMLResponse)
async def console_docs_redirect(_: None = Depends(require_admin)) -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


@console_router.get("/openapi.json", response_class=JSONResponse)
async def console_openapi(
    request: Request, _: None = Depends(require_admin)
) -> JSONResponse:
    return JSONResponse(request.app.openapi())
