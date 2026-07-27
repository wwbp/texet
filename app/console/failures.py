"""Operational failure feed: replies that gave up, and prompts that went missing.

Two different kinds of bad news, deliberately on one page because an operator
watching a running study wants a single place to look:

  - Failed replies are fatal — the participant got nothing. Read straight from
    utterances, the source of truth, rather than mirrored into another table.
  - Prompt issues are non-fatal — the reply went out, but without a section
    that should have been in it. These are the silent ones.
"""

from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONSOLE_PREFIX, UTTERANCE_STATUS_FAILED
from app.console.core import _escape, _serialize_datetime, console_router, require_admin
from app.db import get_async_session
from app.models.response import PromptIssue, Utterance

_ROW_LIMIT = 100


def _failed_rows(replies: list[Utterance]) -> str:
    if not replies:
        return '<tr><td colspan="5">No failed replies.</td></tr>'
    return "\n".join(
        f"""
        <tr>
          <td>{_escape(_serialize_datetime(reply.timestamp))}</td>
          <td class="mono">{_escape(reply.speaker_id)}</td>
          <td class="mono">{_escape(reply.id)}</td>
          <td>{reply.attempts}</td>
          <td class="detail">{_escape(reply.error or "(no error recorded)")}</td>
        </tr>
        """
        for reply in replies
    )


def _issue_rows(issues: list[PromptIssue]) -> str:
    if not issues:
        return '<tr><td colspan="4">No prompt issues.</td></tr>'
    return "\n".join(
        f"""
        <tr>
          <td>{_escape(_serialize_datetime(issue.created_at))}</td>
          <td class="mono">{_escape(issue.kind)}</td>
          <td class="mono">{_escape(issue.user_id)}</td>
          <td class="detail">{_escape(issue.detail)}</td>
        </tr>
        """
        for issue in issues
    )


def _render_failures_page(replies: list[Utterance], issues: list[PromptIssue]) -> HTMLResponse:
    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Failures</title>
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
            --warn:#9a6700;
          }}
          *{{box-sizing:border-box}}
          body{{
            margin:0;
            font-family:"SF Pro Text","Segoe UI","Helvetica Neue","Noto Sans",sans-serif;
            color:var(--text);
            background:var(--bg);
          }}
          .wrap{{max-width:1180px;margin:0 auto;padding:40px 20px 56px}}
          h1{{margin:0 0 6px;font-size:24px;letter-spacing:-.02em}}
          h2{{margin:28px 0 4px;font-size:16px}}
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .muted{{color:var(--muted);font-size:13px}}
          .count{{font-weight:600}}
          .fatal{{color:var(--error)}}
          .warn{{color:var(--warn)}}
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
            padding:9px 12px;
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
          .detail{{
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            font-size:12px;
            white-space:pre-wrap;
            word-break:break-word;
          }}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Failures</h1>
          <p>Newest {_ROW_LIMIT} of each. Check this during a running study —
             both kinds are invisible to the participant-facing flow.</p>

          <h2 class="fatal">Failed replies
            <span class="count">({len(replies)})</span></h2>
          <p class="muted">The participant received nothing. These have already exhausted
             their retries; a reply still retrying does not appear here.</p>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Bot speaker</th>
                <th>Utterance</th>
                <th>Attempts</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {_failed_rows(replies)}
            </tbody>
          </table>

          <h2 class="warn">Prompt issues
            <span class="count">({len(issues)})</span></h2>
          <p class="muted">The reply was sent, but a prompt section was missing or the
             request metadata was malformed. Usually a hub contract problem or an
             unauthored study day.</p>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Participant</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {_issue_rows(issues)}
            </tbody>
          </table>

          <p class="muted" style="margin-top:20px;">
            <a href="{CONSOLE_PREFIX}">Back to console</a>
          </p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html.strip())


@console_router.get("/failures", response_class=HTMLResponse)
async def console_failures(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    failed = await session.execute(
        select(Utterance)
        .where(Utterance.status == UTTERANCE_STATUS_FAILED)
        .order_by(Utterance.timestamp.desc())
        .limit(_ROW_LIMIT)
    )
    issues = await session.execute(
        select(PromptIssue).order_by(PromptIssue.created_at.desc()).limit(_ROW_LIMIT)
    )
    return _render_failures_page(list(failed.scalars().all()), list(issues.scalars().all()))
