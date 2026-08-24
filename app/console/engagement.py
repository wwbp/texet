"""Chatbot engagement, per participant per pinged day.

The same rows GET /engagement returns, rendered as a table. A day appears only
if the hub pinged the participant on it; engaged means they sent at least one
message that same local day.
"""

from __future__ import annotations

import datetime

from fastapi import Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONSOLE_PREFIX
from app.console.core import _escape, console_router, require_admin
from app.db import get_async_session
from app.engagement.service import EngagementDay, compute_engagement

_DEFAULT_WINDOW_DAYS = 30


def _parse_date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _token_cell(value: int | None) -> str:
    # An em dash, not 0: nothing that day carried provider-reported usage.
    return "—" if value is None else f"{value:,}"


def _rows_html(rows: list[EngagementDay]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="muted">No pinged days in this range.</td></tr>'
    cells = []
    for row in rows:
        badge = '<span class="yes">yes</span>' if row.engaged else '<span class="no">no</span>'
        cells.append(
            "<tr>"
            f"<td>{_escape(row.participant_id)}</td>"
            f"<td>{_escape(row.date.isoformat())}</td>"
            f"<td>{badge}</td>"
            f"<td class='num'>{row.utterance_count}</td>"
            f"<td class='num'>{_token_cell(row.token_count)}</td>"
            "</tr>"
        )
    return "".join(cells)


def _summary_line(rows: list[EngagementDay]) -> str:
    if not rows:
        return ""
    engaged = sum(1 for row in rows if row.engaged)
    participants = len({row.participant_id for row in rows})
    pct = (engaged / len(rows)) * 100
    return (
        f"{len(rows)} pinged days across {participants} participants — "
        f"{engaged} engaged ({pct:.0f}%)."
    )


def _render(
    rows: list[EngagementDay],
    start: datetime.date,
    end: datetime.date,
    error_message: str | None = None,
) -> HTMLResponse:
    notice = f'<p class="error">{_escape(error_message)}</p>' if error_message else ""
    summary = _summary_line(rows)
    summary_block = f'<p class="notice">{_escape(summary)}</p>' if summary else ""

    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Texet Console - Engagement</title>
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
          p{{margin:0;color:var(--muted);line-height:1.5}}
          .muted{{color:var(--muted);font-size:13px}}
          .error{{margin-top:12px;color:var(--error);font-size:13px}}
          .notice{{margin-top:12px;color:var(--ok);font-size:13px;font-weight:600}}
          form{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:16px}}
          label{{display:block;font-size:12px;color:var(--muted)}}
          input{{padding:8px 10px;border:1px solid var(--border);border-radius:10px}}
          button{{
            padding:8px 14px;
            border-radius:10px;
            border:1px solid var(--accent);
            background:var(--accent);
            color:#fff;
            font-weight:600;
            cursor:pointer;
          }}
          a.api{{color:var(--accent);font-size:13px}}
          table{{
            width:100%;
            border-collapse:collapse;
            margin-top:16px;
            border:1px solid var(--border);
            background:var(--panel);
            border-radius:12px;
            overflow:hidden;
          }}
          th,td{{padding:9px 12px;text-align:left;font-size:13px;
                border-bottom:1px solid var(--border)}}
          th{{background:#faf8f5;font-weight:600;color:var(--muted)}}
          tr:last-child td{{border-bottom:none}}
          td.num{{text-align:right;font-variant-numeric:tabular-nums}}
          .yes{{color:var(--ok);font-weight:600}}
          .no{{color:var(--error);font-weight:600}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>Engagement</h1>
          <p class="muted">
            One row per participant per day the chatbot pinged them. Engaged means they
            sent at least one message that day, in their own timezone. Token counts come
            from the provider and read &mdash; for replies generated before usage was recorded.
          </p>
          {notice}
          {summary_block}
          <form method="get" action="{CONSOLE_PREFIX}/engagement">
            <div>
              <label for="start">Start</label>
              <input id="start" type="date" name="start" value="{_escape(start.isoformat())}" />
            </div>
            <div>
              <label for="end">End</label>
              <input id="end" type="date" name="end" value="{_escape(end.isoformat())}" />
            </div>
            <button type="submit">Apply</button>
            <a class="api"
               href="/engagement?start={_escape(start.isoformat())}&amp;end={_escape(end.isoformat())}">
              same rows as JSON
            </a>
          </form>
          <table>
            <thead>
              <tr>
                <th>Participant</th>
                <th>Date</th>
                <th>Engaged</th>
                <th style="text-align:right">Utterances</th>
                <th style="text-align:right">Tokens</th>
              </tr>
            </thead>
            <tbody>{_rows_html(rows)}</tbody>
          </table>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@console_router.get("/engagement", response_class=HTMLResponse)
async def console_engagement(
    start: str | None = None,
    end: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    today = datetime.datetime.now(datetime.UTC).date()
    default_start = today - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS)

    parsed_start = _parse_date(start) if start else default_start
    parsed_end = _parse_date(end) if end else today

    if parsed_start is None or parsed_end is None:
        return _render([], default_start, today, "Dates must be YYYY-MM-DD.")
    if parsed_start > parsed_end:
        return _render([], parsed_start, parsed_end, "Start must not be after end.")

    rows = await compute_engagement(session, start=parsed_start, end=parsed_end)
    return _render(rows, parsed_start, parsed_end)
