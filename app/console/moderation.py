from __future__ import annotations

from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import CONSOLE_PREFIX
from app.console.core import _escape, console_router, require_admin
from app.db import get_async_session
from app.models.admin import ModerationSettings
from app.response.crud import MODERATION_SETTINGS_ID, get_moderation_settings

_THRESHOLD_PREFIX = "threshold:"


def _render_moderation_page(
    email_enabled: bool,
    thresholds: dict[str, float],
    error_message: str | None = None,
) -> HTMLResponse:
    error_block = ""
    if error_message:
        error_block = f'<p class="error">{_escape(error_message)}</p>'

    rows = "\n".join(
        f"""
        <tr>
          <td class="mono">{_escape(category)}</td>
          <td>
            <input type="number" name="{_THRESHOLD_PREFIX}{_escape(category)}"
                   value="{value:g}" min="0" max="1" step="0.01" required />
          </td>
          <td class="muted">{"off" if value >= 1.0 else "enforced"}</td>
        </tr>
        """
        for category, value in sorted(thresholds.items())
    )

    checked = " checked" if email_enabled else ""

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Moderation</title>
        <style>
          :root{{
            color-scheme:light;
            --bg:#f6f3ef;
            --panel:#fff;
            --text:#1f2328;
            --muted:#5f6b7a;
            --accent:#1c5d99;
            --border:#e5e1da;
            --error:#b42318;
          }}
          *{{box-sizing:border-box}}
          body{{
            margin:0;
            font-family:"SF Pro Text","Segoe UI","Helvetica Neue","Noto Sans",sans-serif;
            color:var(--text);
            background:var(--bg);
          }}
          .wrap{{max-width:980px;margin:0 auto;padding:40px 20px 56px}}
          h1{{margin:0 0 6px;font-size:24px;letter-spacing:-.02em}}
          h2{{margin:24px 0 10px;font-size:16px}}
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .muted{{color:var(--muted);font-size:13px}}
          .error{{margin-top:12px;color:var(--error);font-size:13px}}
          form{{margin:0}}
          .lever{{
            margin-top:12px;
            padding:12px 14px;
            background:var(--panel);
            border:1px solid var(--border);
            border-radius:12px;
            display:flex;
            gap:10px;
            align-items:flex-start;
          }}
          input[type=number]{{
            width:110px;
            padding:6px 8px;
            border:1px solid var(--border);
            border-radius:8px;
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            font-size:13px;
          }}
          button{{
            margin-top:12px;
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
            vertical-align:middle;
          }}
          th{{font-size:12px;color:var(--muted);font-weight:600}}
          .mono{{
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            word-break:break-word;
          }}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Moderation</h1>
          <p>Every moderation setting, in one place. Changes apply to the next message.</p>
          {error_block}

          <form method="post" action="{CONSOLE_PREFIX}/moderation">
            <h2>Alert email</h2>
            <label class="lever">
              <input type="checkbox" name="email_enabled"{checked} />
              <span>
                Email the moderation alert list when a participant message is withheld.
                <br /><span class="muted">Recipients come from MODERATION_ALERT_EMAILS.</span>
              </span>
            </label>

            <h2>Thresholds</h2>
            <p class="muted">
              A message is withheld when its score is <em>above</em> the threshold.
              Scores are bounded at 1.0, so 1.0 is an off switch.
            </p>
            <table>
              <thead>
                <tr><th>Category</th><th>Threshold</th><th>State</th></tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>

            <button type="submit">Save</button>
          </form>

          <p class="muted" style="margin-top:16px;">
            <a href="{CONSOLE_PREFIX}">Back to console</a>
          </p>
        </div>
      </body>
    </html>
    """
    status_code = 400 if error_message else 200
    return HTMLResponse(html.strip(), status_code=status_code)


@console_router.get("/moderation", response_class=HTMLResponse)
async def console_moderation(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return _render_moderation_page(*await get_moderation_settings(session))


@console_router.post("/moderation", response_class=HTMLResponse)
async def console_moderation_save(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    email_enabled = bool(form.get("email_enabled"))

    thresholds: dict[str, float] = {}
    for field, raw in form.multi_items():
        if not field.startswith(_THRESHOLD_PREFIX):
            continue
        category = field[len(_THRESHOLD_PREFIX) :]
        try:
            value = float(str(raw))
        except ValueError:
            current = await get_moderation_settings(session)
            return _render_moderation_page(
                *current, f"{category}: threshold must be a number between 0 and 1."
            )
        if not 0.0 <= value <= 1.0:
            current = await get_moderation_settings(session)
            return _render_moderation_page(
                *current, f"{category}: threshold must be between 0 and 1."
            )
        thresholds[category] = value

    async with session.begin():
        await session.merge(
            ModerationSettings(
                id=MODERATION_SETTINGS_ID,
                email_enabled=email_enabled,
                thresholds=thresholds,
            )
        )

    return _render_moderation_page(*await get_moderation_settings(session))
