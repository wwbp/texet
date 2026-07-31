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
            background:radial-gradient(
              1200px 600px at 10% -10%,
              #fff8ee 0%,
              var(--bg) 55%,
              #f0f4f7 100%
            );
          }}
          .wrap{{max-width:900px;margin:0 auto;padding:40px 20px 56px}}
          h1{{margin:0 0 6px;font-size:28px;letter-spacing:-.02em}}
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .grid{{
            display:grid;
            grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
            gap:16px;
            margin-top:24px;
          }}
          .card{{
            display:block;
            padding:16px;
            border:1px solid var(--border);
            border-radius:14px;
            text-decoration:none;
            color:inherit;
            background:var(--panel);
            transition:transform .12s ease,box-shadow .12s ease,border-color .12s ease;
          }}
          .card:hover{{
            transform:translateY(-2px);
            border-color:#d8d2c6;
            box-shadow:0 12px 28px rgba(31,35,40,.12);
          }}
          .card h2{{margin:6px 0 6px;font-size:16px}}
          .card span{{font-size:12px;font-weight:600;letter-spacing:.02em;color:var(--accent)}}
          .note{{margin-top:20px;font-size:12px;color:var(--muted)}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Texet Console</h1>
          <p>Dashboard for data, access, and exports. Choose a module.</p>
          <section class="grid">
            <a class="card" href="{CONSOLE_PREFIX}/admin">
              <span>Overview</span>
              <h2>Admin</h2>
              <p>Read-only views of speakers, conversations, and utterances.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/api-keys">
              <span>Access</span>
              <h2>API Keys</h2>
              <p>Create and rotate keys for internal teams.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/system-prompts">
              <span>Assistant</span>
              <h2>System Prompts</h2>
              <p>Add, update, and delete prompts. Latest created prompt is active.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/daily-prompts">
              <span>Assistant</span>
              <h2>Daily Prompts</h2>
              <p>One prompt per day number. Appended to the system prompt when matched.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/prompt-templates">
              <span>Assistant</span>
              <h2>Prompt Template</h2>
              <p>Layout with placeholders that consolidates every prompt piece into one.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/summarization-prompts">
              <span>Assistant</span>
              <h2>Summarization Prompts</h2>
              <p>Instruction for the weekly summarizer. Latest created prompt is active.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/summaries">
              <span>Operations</span>
              <h2>Weekly Summaries</h2>
              <p>Force a summary run for every participant in a chosen week.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/failures">
              <span>Operations</span>
              <h2>Failures</h2>
              <p>Replies that gave up, and prompts that silently went missing.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/exports">
              <span>Data</span>
              <h2>Exports</h2>
              <p>Download verified ConvoKit corpora by time range.</p>
            </a>
            <a class="card" href="{CONSOLE_PREFIX}/docs">
              <span>API</span>
              <h2>API Docs</h2>
              <p>Authenticated testing against the live API.</p>
            </a>
          </section>
          <div class="note">Use test keys/data when validating changes.</div>
        </div>
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
async def console_openapi(request: Request, _: None = Depends(require_admin)) -> JSONResponse:
    return JSONResponse(request.app.openapi())
