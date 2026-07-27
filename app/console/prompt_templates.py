from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import CONSOLE_PREFIX
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.response import InstructionTemplate
from app.response.prompt import (
    DECORATIVE_PLACEHOLDERS,
    DEFAULT_INSTRUCTION_TEMPLATE,
    GATING_PLACEHOLDERS,
)

_PLACEHOLDER_HELP: dict[str, str] = {
    "base": "The active system prompt.",
    "daily_content": "Today's daily prompt, when one matches the day number.",
    "weekly_summary": "Previous week's summary, when one exists.",
    "formatted_time": "The user's local time, e.g. Sunday, June 7, 2026 at 2:30 PM (UTC-5).",
    "day_suffix": "Renders ' (Day 26)' when a day number is known, otherwise nothing.",
}

_REQUIRED_PLACEHOLDER = "{base}"


def _placeholder_rows() -> str:
    rows = []
    for name in GATING_PLACEHOLDERS + DECORATIVE_PLACEHOLDERS:
        kind = "drops its paragraph when empty" if name in GATING_PLACEHOLDERS else "decorative"
        rows.append(
            f'<tr><td class="mono">{{{name}}}</td>'
            f"<td>{_escape(_PLACEHOLDER_HELP[name])}</td>"
            f'<td class="muted">{kind}</td></tr>'
        )
    return "\n".join(rows)


def _render_prompt_templates_page(
    templates: list[InstructionTemplate], error_message: str | None = None
) -> HTMLResponse:
    error_block = ""
    if error_message:
        error_block = f'<p class="error">{_escape(error_message)}</p>'

    if templates:
        rows = "\n".join(
            f"""
            <tr>
              <td class="mono">{_escape(template.id)}</td>
              <td>{_escape(_serialize_datetime(template.created_at))}</td>
              <td>
                <form method="post"
                      action="{CONSOLE_PREFIX}/prompt-templates/{_escape(template.id)}">
                  <textarea name="template" rows="14"
                            required>{_escape(template.template)}</textarea>
                  <div class="actions">
                    <button type="submit">Update</button>
                  </div>
                </form>
              </td>
              <td>
                <form method="post"
                      action="{CONSOLE_PREFIX}/prompt-templates/{_escape(template.id)}/delete">
                  <button type="submit" class="danger">Delete</button>
                </form>
              </td>
            </tr>
            """
            for template in templates
        )
    else:
        rows = (
            '<tr><td colspan="4">No templates yet — the built-in default is in use '
            "(shown in the editor below).</td></tr>"
        )

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Prompt Template</title>
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
          .create-form{{margin-top:12px}}
          textarea{{
            width:100%;
            min-height:220px;
            padding:10px;
            border:1px solid var(--border);
            border-radius:10px;
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            font-size:13px;
            line-height:1.5;
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
            word-break:break-word;
          }}
          .actions{{display:flex;gap:8px;align-items:center}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Prompt Template</h1>
          <p>Layout that consolidates the system prompt, daily prompt, weekly summary, and
             local time into the instruction sent with every reply.</p>
          <p class="muted">Latest created template (top row) is used by the system.</p>
          {error_block}

          <h2>Placeholders</h2>
          <p class="muted">
            A paragraph — text between blank lines — is dropped entirely when a placeholder
            inside it has no value, so optional sections disappear instead of leaving an empty
            label. Unrecognised placeholders are left in the prompt as literal text.
            {_REQUIRED_PLACEHOLDER} is required.
          </p>
          <table>
            <thead>
              <tr>
                <th>Placeholder</th>
                <th>Value</th>
                <th>Behaviour</th>
              </tr>
            </thead>
            <tbody>
              {_placeholder_rows()}
            </tbody>
          </table>

          <h2>Add template</h2>
          <form class="create-form" method="post" action="{CONSOLE_PREFIX}/prompt-templates">
            <textarea name="template" rows="16"
                      required>{_escape(DEFAULT_INSTRUCTION_TEMPLATE)}</textarea>
            <div class="actions">
              <button type="submit">Add template</button>
            </div>
          </form>

          <h2>Existing templates</h2>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Created</th>
                <th>Template</th>
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


async def _list_templates(session: AsyncSession) -> list[InstructionTemplate]:
    result = await session.execute(
        select(InstructionTemplate).order_by(InstructionTemplate.created_at.desc()).limit(100)
    )
    return list(result.scalars().all())


def _validation_error(value: str) -> str | None:
    if not value:
        return "Template is required."
    if _REQUIRED_PLACEHOLDER not in value:
        return f"Template must contain the {_REQUIRED_PLACEHOLDER} placeholder."
    return None


@console_router.get("/prompt-templates", response_class=HTMLResponse)
async def console_prompt_templates(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return _render_prompt_templates_page(await _list_templates(session))


@console_router.post("/prompt-templates", response_class=HTMLResponse)
async def console_prompt_templates_create(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    value = str(form.get("template") or "").strip()
    error = _validation_error(value)
    if error:
        return _render_prompt_templates_page(await _list_templates(session), error)

    async with session.begin():
        session.add(InstructionTemplate(template=value))
    return _render_prompt_templates_page(await _list_templates(session))


@console_router.post("/prompt-templates/{template_id}", response_class=HTMLResponse)
async def console_prompt_templates_update(
    template_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    value = str(form.get("template") or "").strip()
    error = _validation_error(value)
    if error:
        return _render_prompt_templates_page(await _list_templates(session), error)

    async with session.begin():
        template = await session.get(InstructionTemplate, template_id)
        if not template:
            return _render_prompt_templates_page(
                await _list_templates(session), "Template not found."
            )
        template.template = value

    return _render_prompt_templates_page(await _list_templates(session))


@console_router.post("/prompt-templates/{template_id}/delete", response_class=HTMLResponse)
async def console_prompt_templates_delete(
    template_id: str,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    async with session.begin():
        template = await session.get(InstructionTemplate, template_id)
        if not template:
            return _render_prompt_templates_page(
                await _list_templates(session), "Template not found."
            )
        await session.delete(template)

    return _render_prompt_templates_page(await _list_templates(session))
