"""Manual trigger for the weekly summarizer.

The scheduled job only ever targets last week and skips participants that
already have a summary, so seeded or backfilled data can never be summarised
by waiting for the cron. This page forces a run for a chosen week and shows
what came back.
"""

from __future__ import annotations

import datetime

from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import CONSOLE_PREFIX
from app.console.core import _escape, console_router, require_admin
from app.db import get_async_session, get_sessionmaker
from app.models.response import WeeklySummary
from app.response.utils import week_start_utc
from app.summary.service import ForceSummaryResult, force_weekly_summaries

_ROW_LIMIT = 50


def _previous_week_start() -> datetime.date:
    now = datetime.datetime.now(datetime.UTC)
    return week_start_utc(now) - datetime.timedelta(days=7)


def _parse_week_start(value: str) -> datetime.date | None:
    """Any date in the week is accepted and snapped to that week's Sunday."""
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return week_start_utc(datetime.datetime.combine(parsed, datetime.time.min, tzinfo=datetime.UTC))


def _summary_rows(summaries: list[WeeklySummary]) -> str:
    if not summaries:
        return '<tr><td colspan="3">No summaries yet.</td></tr>'
    return "\n".join(
        f"""
        <tr>
          <td>{_escape(summary.week_start.isoformat())}</td>
          <td class="mono">{_escape(summary.user_id)}</td>
          <td class="detail">{_escape(summary.summary)}</td>
        </tr>
        """
        for summary in summaries
    )


def _render_summaries_page(
    summaries: list[WeeklySummary],
    week_start: datetime.date,
    result: ForceSummaryResult | None = None,
    error_message: str | None = None,
) -> HTMLResponse:
    notice_block = ""
    if error_message:
        notice_block = f'<p class="error">{_escape(error_message)}</p>'
    elif result:
        notice_block = (
            f'<p class="notice">Week of {_escape(week_start.isoformat())}: '
            f"{result.users} participants active, {result.generated} generated, "
            f"{result.failed} failed.</p>"
        )

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Weekly Summaries</title>
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
            --ok:#1a7f57;
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
          .notice{{margin-top:12px;color:var(--ok);font-size:13px;font-weight:600}}
          form{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:12px}}
          label{{display:block;font-size:12px;color:var(--muted)}}
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
            vertical-align:top;
          }}
          th{{font-size:12px;color:var(--muted);font-weight:600}}
          .mono{{
            font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
            word-break:break-word;
          }}
          .detail{{white-space:pre-wrap}}
          a{{color:var(--accent);text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Weekly Summaries</h1>
          <p>
            Force a summary for every participant with messages in the chosen week,
            overwriting any summary already stored for it.
          </p>
          <p class="muted">
            Any date in the week works — it is snapped to that week's Sunday (UTC).
            One LLM call per participant, run inline, so a large cohort takes a while.
          </p>
          {notice_block}
          <form method="post" action="{CONSOLE_PREFIX}/summaries">
            <div>
              <label for="week_start">Week</label>
              <input type="date" id="week_start" name="week_start"
                     value="{_escape(week_start.isoformat())}" required />
            </div>
            <button type="submit">Force generate for all users</button>
          </form>

          <h2>Recent summaries</h2>
          <table>
            <thead>
              <tr>
                <th>Week Start</th>
                <th>User</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {_summary_rows(summaries)}
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


async def _recent_summaries(session: AsyncSession) -> list[WeeklySummary]:
    result = await session.execute(
        select(WeeklySummary)
        .order_by(WeeklySummary.week_start.desc(), WeeklySummary.user_id)
        .limit(_ROW_LIMIT)
    )
    return list(result.scalars().all())


@console_router.get("/summaries", response_class=HTMLResponse)
async def console_summaries(
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return _render_summaries_page(await _recent_summaries(session), _previous_week_start())


@console_router.post("/summaries", response_class=HTMLResponse)
async def console_summaries_force(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    form = await request.form()
    week_start = _parse_week_start(str(form.get("week_start") or "").strip())
    if week_start is None:
        return _render_summaries_page(
            await _recent_summaries(session),
            _previous_week_start(),
            error_message="Invalid week start date.",
        )

    result = await force_weekly_summaries(get_sessionmaker(), week_start)
    return _render_summaries_page(await _recent_summaries(session), week_start, result=result)
