import json

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth.api_keys import create_api_key
from app.auth.service import API_KEY_SCOPE_RESPONSE, API_KEY_SCOPES
from app.config import CONSOLE_PREFIX
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.auth import ApiKey


def _revoke_form(key: ApiKey) -> str:
    if not key.is_active:
        return ""
    # The browser undoes _escape before running onsubmit, so the name needs json.dumps
    # too: _escape alone would let a quote in it break out of the JS string.
    message = json.dumps(
        f"Revoke API key {key.name or key.key_prefix} (scope: {key.scope})? "
        "Clients using it stop working immediately."
    )
    return (
        f'<form method="post" action="{CONSOLE_PREFIX}/api-keys/{_escape(key.id)}/revoke" '
        f'onsubmit="return confirm({_escape(message)})" style="margin:0">'
        '<button type="submit" class="danger">Revoke</button>'
        "</form>"
    )


def _render_api_keys_page(
    keys: list[ApiKey], generated_key: str | None = None, error_message: str | None = None
) -> HTMLResponse:
    error_block = f'<p class="error">{_escape(error_message)}</p>' if error_message else ""
    generated_block = ""
    if generated_key:
        generated_block = f"""
        <section class="panel">
          <h2>New API key</h2>
          <p class="mono">{_escape(generated_key)}</p>
          <p class="muted">Copy it now; it will not be shown again.</p>
        </section>
        """

    if keys:
        rows = "\n".join(
            f"<tr>"
            f"<td>{_escape(key.id)}</td>"
            f"<td>{_escape(key.name or '')}</td>"
            f"<td>{_escape(key.key_prefix)}</td>"
            f"<td>{_escape(key.scope)}</td>"
            f"<td>{'yes' if key.is_active else 'no'}</td>"
            f"<td>{_escape(_serialize_datetime(key.created_at))}</td>"
            f"<td>{_escape(_serialize_datetime(key.last_used_at))}</td>"
            f"<td>{_revoke_form(key)}</td>"
            f"</tr>"
            for key in keys
        )
    else:
        rows = '<tr><td colspan="8">No API keys yet.</td></tr>'

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - API Keys</title>
        <style>
          :root{{
            color-scheme:light;
            --bg:#f6f3ef;
            --panel:#fff;
            --text:#1f2328;
            --muted:#5f6b7a;
            --accent:#1c5d99;
            --border:#e5e1da;
          }}
          *{{box-sizing:border-box}}
          body{{
            margin:0;
            font-family:"SF Pro Text","Segoe UI","Helvetica Neue","Noto Sans",sans-serif;
            color:var(--text);
            background:var(--bg);
          }}
          .wrap{{max-width:900px;margin:0 auto;padding:40px 20px 56px}}
          h1{{margin:0 0 6px;font-size:24px;letter-spacing:-.02em}}
          h2{{margin:24px 0 10px;font-size:16px}}
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .muted{{color:var(--muted);font-size:13px}}
          .error{{margin-top:12px;color:#b42318;font-size:13px}}
          .panel{{
            margin:16px 0;
            padding:14px;
            border:1px solid var(--border);
            border-radius:12px;
            background:var(--panel);
          }}
          .mono{{
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            font-size:13px;
            word-break:break-all;
            color:#0f172a;
          }}
          form{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:12px}}
          label{{font-size:12px;color:var(--muted)}}
          input{{
            padding:8px 10px;
            border:1px solid var(--border);
            border-radius:10px;
            min-width:220px;
          }}
          button{{
            padding:8px 14px;
            border-radius:10px;
            border:1px solid var(--accent);
            background:var(--accent);
            color:#fff;
            font-weight:600;
            cursor:pointer;
          }}
          button.danger{{border-color:#b42318;background:#b42318}}
          table{{
            width:100%;
            border-collapse:collapse;
            margin-top:12px;
            border:1px solid var(--border);
            background:var(--panel);
            border-radius:12px;
            overflow:hidden;
          }}
          th,td{{
            padding:10px 12px;
            border-bottom:1px solid var(--border);
            text-align:left;
            font-size:13px;
          }}
          th{{font-size:12px;color:var(--muted);font-weight:600}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>API Keys</h1>
          <p>Create a key for clients. The full key is shown once.</p>
          {error_block}
          {generated_block}
          <form method="post" action="{CONSOLE_PREFIX}/api-keys">
            <div>
              <label for="name">Name (optional)</label>
              <input type="text" id="name" name="name" />
            </div>
            <div>
              <label for="scope">Scope</label>
              <select id="scope" name="scope">
                <option value="response">response (hub: POST /response)</option>
                <option value="export">export (GET /exports/convokit)</option>
              </select>
            </div>
            <button type="submit">Create key</button>
          </form>
          <h2>Existing keys</h2>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Prefix</th>
                <th>Scope</th>
                <th>Active</th>
                <th>Created</th>
                <th>Last Used</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows}
            </tbody>
          </table>
          <p class="muted" style="margin-top:16px;">
            <a href="{CONSOLE_PREFIX}">Back to console</a>
          </p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html.strip(), status_code=404 if error_message else 200)


@console_router.get("/api-keys", response_class=HTMLResponse)
async def console_api_keys(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50))
    keys = list(result.scalars().all())
    return _render_api_keys_page(keys)


@console_router.post("/api-keys", response_class=HTMLResponse)
async def console_api_keys_create(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    name = str(form.get("name") or "").strip() or None
    scope = str(form.get("scope") or API_KEY_SCOPE_RESPONSE)
    if scope not in API_KEY_SCOPES:
        raise HTTPException(status_code=400, detail="Unknown scope.")
    async with session.begin():
        key = await create_api_key(session, name=name, scope=scope)

    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50))
    keys = list(result.scalars().all())
    return _render_api_keys_page(keys, generated_key=key)


@console_router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
async def console_api_keys_revoke(
    key_id: str,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    # Never delete the row: admin_exports.meta.api_key_id refers to it for audit.
    async with session.begin():
        key = await session.get(ApiKey, key_id)
        if key:
            key.is_active = False

    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50))
    keys = list(result.scalars().all())
    return _render_api_keys_page(keys, error_message=None if key else "API key not found.")
