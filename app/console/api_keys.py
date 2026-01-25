from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.auth.api_keys import create_api_key
from app.config import CONSOLE_PREFIX
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.auth import ApiKey


def _render_api_keys_page(keys: list[ApiKey], generated_key: str | None = None) -> HTMLResponse:
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
            f"<td>{'yes' if key.is_active else 'no'}</td>"
            f"<td>{_escape(_serialize_datetime(key.created_at))}</td>"
            f"<td>{_escape(_serialize_datetime(key.last_used_at))}</td>"
            f"</tr>"
            for key in keys
        )
    else:
        rows = '<tr><td colspan="6">No API keys yet.</td></tr>'

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
          {generated_block}
          <form method="post" action="{CONSOLE_PREFIX}/api-keys">
            <div>
              <label for="name">Name (optional)</label>
              <input type="text" id="name" name="name" />
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
                <th>Active</th>
                <th>Created</th>
                <th>Last Used</th>
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
    return HTMLResponse(html.strip())


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
    async with session.begin():
        key = await create_api_key(session, name=name)

    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()).limit(50))
    keys = list(result.scalars().all())
    return _render_api_keys_page(keys, generated_key=key)
