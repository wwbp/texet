from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth.api_keys import create_api_key
from app.config import CONSOLE_PREFIX
from app.console.core import console_router, require_admin, _escape, _serialize_datetime
from app.db import get_async_session
from app.models.auth import ApiKey


def _render_api_keys_page(
    keys: list[ApiKey], generated_key: str | None = None
) -> HTMLResponse:
    generated_block = ""
    if generated_key:
        generated_block = f"""
        <section>
          <h2>New API key</h2>
          <p><code>{_escape(generated_key)}</code></p>
          <p>Copy it now; it will not be shown again.</p>
        </section>
        """

    if keys:
        rows = "\n".join(
            f"<tr>"
            f"<td>{_escape(key.id)}</td>"
            f"<td>{_escape(key.name or '')}</td>"
            f"<td>{_escape(key.key_prefix)}</td>"
            f"<td>{'yes' if key.is_active else 'no'}</td>"
            f"<td>{_escape(_serialize_datetime(key.created_at))}</td>"
            f"<td>{_escape(_serialize_datetime(key.last_used_at))}</td>"
            f"</tr>"
            for key in keys
        )
    else:
        rows = "<tr><td colspan=\"6\">No API keys yet.</td></tr>"

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - API Keys</title>
      </head>
      <body>
        <h1>API Keys</h1>
        <p>Create a new API key for clients. The full key is shown once.</p>
        {generated_block}
        <form method="post" action="{CONSOLE_PREFIX}/api-keys">
          <label for="name">Name (optional)</label>
          <input type="text" id="name" name="name" />
          <button type="submit">Create key</button>
        </form>
        <h2>Existing keys</h2>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Name</th>
              <th>Prefix</th>
              <th>Active</th>
              <th>Created</th>
              <th>Last Used</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
        <p><a href="{CONSOLE_PREFIX}">Back to console</a></p>
      </body>
    </html>
    """
    return HTMLResponse(html.strip())


@console_router.get("/api-keys", response_class=HTMLResponse)
async def console_api_keys(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50)
    )
    keys = result.scalars().all()
    return _render_api_keys_page(keys)


@console_router.post("/api-keys", response_class=HTMLResponse)
async def console_api_keys_create(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    name = str(form.get("name") or "").strip() or None
    async with session.begin():
        key = await create_api_key(session, name=name)

    result = await session.execute(
        select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50)
    )
    keys = result.scalars().all()
    return _render_api_keys_page(keys, generated_key=key)
