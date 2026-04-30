from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import CONSOLE_PREFIX
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.response import DailyPrompt

_INPUT_STYLE = (
    "width:100%;padding:6px;border:1px solid var(--border);border-radius:8px;font-size:13px;"
)


def _render_daily_prompts_page(
    prompts: list[DailyPrompt], error_message: str | None = None
) -> HTMLResponse:
    error_block = ""
    if error_message:
        error_block = f'<p class="error">{_escape(error_message)}</p>'

    if prompts:
        rows = "\n".join(
            f"""
            <tr>
              <td class="mono">{_escape(str(prompt.day_identifier))}</td>
              <td>{_escape(_serialize_datetime(prompt.created_at))}</td>
              <td>
                <form method="post"
                      action="{CONSOLE_PREFIX}/daily-prompts/{_escape(prompt.id)}">
                  <textarea name="content" rows="4" required>{_escape(prompt.content)}</textarea>
                  <div class="actions">
                    <button type="submit">Update</button>
                  </div>
                </form>
              </td>
              <td>
                <form method="post"
                      action="{CONSOLE_PREFIX}/daily-prompts/{_escape(prompt.id)}/delete">
                  <button type="submit" class="danger">Delete</button>
                </form>
              </td>
            </tr>
            """
            for prompt in prompts
        )
    else:
        rows = '<tr><td colspan="4">No daily prompts yet.</td></tr>'

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Daily Prompts</title>
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
          .create-form{{margin-top:12px;display:flex;flex-direction:column;gap:6px}}
          textarea{{
            width:100%;
            min-height:88px;
            padding:10px;
            border:1px solid var(--border);
            border-radius:10px;
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            font-size:13px;
          }}
          input[type=number]{{
            width:140px;
            padding:6px;
            border:1px solid var(--border);
            border-radius:8px;
            font-size:13px;
          }}
          button{{
            margin-top:8px;
            padding:8px 14px;
            border-radius:10px;
            border:1px solid var(--accent);
            background:var(--accent);
            color:#fff;
            font-weight:600;
            cursor:pointer;
            width:fit-content;
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
            vertical-align:top;
          }}
          th{{font-size:12px;color:var(--muted);font-weight:600}}
          .mono{{
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
          }}
          .actions{{display:flex;gap:8px;align-items:center}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Daily Prompts</h1>
          <p>One prompt per day number. The matching prompt is appended to the system prompt
             when a request includes that <code>day_identifier</code> in its metadata.</p>
          {error_block}

          <h2>Add prompt</h2>
          <form class="create-form" method="post" action="{CONSOLE_PREFIX}/daily-prompts">
            <label style="font-size:13px;font-weight:600;">
              Day number
              <input type="number" name="day_identifier" min="1" required
                     placeholder="e.g. 1" />
            </label>
            <textarea name="content" rows="4" required
                      placeholder="Prompt content for this day"></textarea>
            <div class="actions">
              <button type="submit">Add prompt</button>
            </div>
          </form>

          <h2>Existing prompts</h2>
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th>Created</th>
                <th>Content</th>
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
    status_code = 400 if error_message else 200
    return HTMLResponse(html.strip(), status_code=status_code)


async def _list_prompts(session: AsyncSession) -> list[DailyPrompt]:
    result = await session.execute(select(DailyPrompt).order_by(DailyPrompt.day_identifier.asc()))
    return list(result.scalars().all())


@console_router.get("/daily-prompts", response_class=HTMLResponse)
async def console_daily_prompts(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return _render_daily_prompts_page(await _list_prompts(session))


@console_router.post("/daily-prompts", response_class=HTMLResponse)
async def console_daily_prompts_create(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    content = str(form.get("content") or "").strip()
    raw_id = str(form.get("day_identifier") or "").strip()

    if not content:
        return _render_daily_prompts_page(await _list_prompts(session), "Content is required.")
    if not raw_id:
        return _render_daily_prompts_page(await _list_prompts(session), "Day number is required.")
    try:
        day_identifier = int(raw_id)
        if day_identifier < 1:
            raise ValueError
    except ValueError:
        return _render_daily_prompts_page(
            await _list_prompts(session), "Day number must be a positive integer."
        )

    try:
        async with session.begin():
            session.add(DailyPrompt(day_identifier=day_identifier, content=content))
    except IntegrityError:
        return _render_daily_prompts_page(
            await _list_prompts(session),
            f"A prompt for day {day_identifier} already exists.",
        )

    return _render_daily_prompts_page(await _list_prompts(session))


@console_router.post("/daily-prompts/{prompt_id}", response_class=HTMLResponse)
async def console_daily_prompts_update(
    prompt_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    content = str(form.get("content") or "").strip()

    if not content:
        return _render_daily_prompts_page(await _list_prompts(session), "Content is required.")

    async with session.begin():
        prompt = await session.get(DailyPrompt, prompt_id)
        if not prompt:
            return _render_daily_prompts_page(await _list_prompts(session), "Prompt not found.")
        prompt.content = content

    return _render_daily_prompts_page(await _list_prompts(session))


@console_router.post("/daily-prompts/{prompt_id}/delete", response_class=HTMLResponse)
async def console_daily_prompts_delete(
    prompt_id: str,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    async with session.begin():
        prompt = await session.get(DailyPrompt, prompt_id)
        if not prompt:
            return _render_daily_prompts_page(await _list_prompts(session), "Prompt not found.")
        await session.delete(prompt)

    return _render_daily_prompts_page(await _list_prompts(session))
