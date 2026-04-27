from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import BEDROCK_DEFAULT_MODEL, CONSOLE_PREFIX
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.response import SystemPrompt


_PROVIDER_OPTIONS = [
    ("openai", "OpenAI"),
    ("bedrock", "Amazon Bedrock"),
]

_MODEL_DEFAULTS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "bedrock": BEDROCK_DEFAULT_MODEL,
}


def _provider_select(selected: str, name: str = "provider") -> str:
    opts = "".join(
        f'<option value="{v}" {"selected" if v == selected else ""}>{label}</option>'
        for v, label in _PROVIDER_OPTIONS
    )
    return f'<select name="{name}" style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;">{opts}</select>'


def _render_system_prompts_page(
    prompts: list[SystemPrompt], error_message: str | None = None
) -> HTMLResponse:
    error_block = ""
    if error_message:
        error_block = f'<p class="error">{_escape(error_message)}</p>'

    if prompts:
        rows = "\n".join(
            f"""
            <tr>
              <td class="mono">{_escape(prompt.id)}</td>
              <td>{_escape(_serialize_datetime(prompt.created_at))}</td>
              <td>
                <form method="post" action="{CONSOLE_PREFIX}/system-prompts/{_escape(prompt.id)}">
                  <textarea name="prompt" rows="3" required>{_escape(prompt.prompt)}</textarea>
                  {_provider_select(prompt.provider)}
                  <input name="model_id" required value="{_escape(prompt.model_id)}"
                         style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;" />
                  <div class="actions">
                    <button type="submit">Update</button>
                  </div>
                </form>
              </td>
              <td>
                <form method="post"
                      action="{CONSOLE_PREFIX}/system-prompts/{_escape(prompt.id)}/delete">
                  <button type="submit" class="danger">Delete</button>
                </form>
              </td>
            </tr>
            """
            for prompt in prompts
        )
    else:
        rows = '<tr><td colspan="4">No system prompts yet.</td></tr>'

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - System Prompts</title>
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
            min-height:88px;
            padding:10px;
            border:1px solid var(--border);
            border-radius:10px;
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
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
            word-break:break-all;
          }}
          .actions{{display:flex;gap:8px;align-items:center}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>System Prompts</h1>
          <p>Prompt library for assistant system prompts.</p>
          <p class="muted">Latest created prompt (top row) is used by the system.</p>
          {error_block}

          <h2>Add prompt</h2>
          <form class="create-form" method="post" action="{CONSOLE_PREFIX}/system-prompts">
            <textarea name="prompt" rows="4" required placeholder="System prompt"></textarea>
            {_provider_select("openai")}
            <input name="model_id" required value="gpt-4o-mini" placeholder="Model ID"
                   style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;margin-top:6px;" />
            <div class="actions">
              <button type="submit">Add prompt</button>
            </div>
          </form>

          <h2>Existing prompts</h2>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Created</th>
                <th>Prompt</th>
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


async def _list_prompts(session: AsyncSession) -> list[SystemPrompt]:
    result = await session.execute(
        select(SystemPrompt).order_by(SystemPrompt.created_at.desc()).limit(100)
    )
    return list(result.scalars().all())


@console_router.get("/system-prompts", response_class=HTMLResponse)
async def console_system_prompts(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return _render_system_prompts_page(await _list_prompts(session))


@console_router.post("/system-prompts", response_class=HTMLResponse)
async def console_system_prompts_create(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    value = str(form.get("prompt") or "").strip()
    provider = str(form.get("provider") or "openai").strip()
    model_id = str(form.get("model_id") or _MODEL_DEFAULTS.get(provider, "gpt-4o-mini")).strip()
    if not value:
        return _render_system_prompts_page(await _list_prompts(session), "Prompt is required.")
    if provider not in _MODEL_DEFAULTS:
        return _render_system_prompts_page(await _list_prompts(session), f"Unknown provider: {provider}")

    async with session.begin():
        session.add(SystemPrompt(prompt=value, provider=provider, model_id=model_id))
    return _render_system_prompts_page(await _list_prompts(session))


@console_router.post("/system-prompts/{prompt_id}", response_class=HTMLResponse)
async def console_system_prompts_update(
    prompt_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    value = str(form.get("prompt") or "").strip()
    provider = str(form.get("provider") or "openai").strip()
    model_id = str(form.get("model_id") or _MODEL_DEFAULTS.get(provider, "gpt-4o-mini")).strip()
    if not value:
        return _render_system_prompts_page(await _list_prompts(session), "Prompt is required.")
    if provider not in _MODEL_DEFAULTS:
        return _render_system_prompts_page(await _list_prompts(session), f"Unknown provider: {provider}")

    async with session.begin():
        prompt = await session.get(SystemPrompt, prompt_id)
        if not prompt:
            return _render_system_prompts_page(await _list_prompts(session), "Prompt not found.")
        prompt.prompt = value
        prompt.provider = provider
        prompt.model_id = model_id

    return _render_system_prompts_page(await _list_prompts(session))


@console_router.post("/system-prompts/{prompt_id}/delete", response_class=HTMLResponse)
async def console_system_prompts_delete(
    prompt_id: str,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    async with session.begin():
        prompt = await session.get(SystemPrompt, prompt_id)
        if not prompt:
            return _render_system_prompts_page(await _list_prompts(session), "Prompt not found.")
        await session.delete(prompt)

    return _render_system_prompts_page(await _list_prompts(session))
