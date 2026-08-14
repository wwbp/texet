"""Build the results workbook from what the database actually recorded.

Every reply carries a texet_generation snapshot — the exact system prompt and
chat history that produced it — so the export is a record of what the model was
given, not a reconstruction of what we think it was given.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))

from personas import (  # noqa: E402
    LIVE_DATES,
    PERSONAS,
    PROBE_DAY,
    PROBE_TEXT,
    STUDY_DAYS,
    TARGET_WEEK_START,
    load_rows,
)

DB_URL = os.environ.get("MEMTEST_DB_URL", "postgresql://texet:change_me@localhost:55432/texet")
OUT_PATH = Path(os.environ.get("MEMTEST_OUT", "SMART_R_Chatbot Memory Test v1 - RESULTS.xlsx"))

CELL_LIMIT = 32000
HEADER_FILL = PatternFill("solid", fgColor="1C5D99")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
BY_KEY = {persona.key: persona for persona in PERSONAS}


def clean(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = ILLEGAL_CHARACTERS_RE.sub("", value)
    if len(text) > CELL_LIMIT:
        return text[:CELL_LIMIT] + f"\n… [truncated, {len(text)} chars total]"
    return text


def render_history(history: list[dict[str, str]]) -> str:
    return "\n\n".join(f"{turn['role']}: {turn['content']}" for turn in history)


def write_sheet(
    book: Workbook, title: str, headers: list[str], rows: list[list[object]], widths: list[int]
) -> None:
    sheet = book.create_sheet(title)
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        sheet.append([clean(value) for value in row])
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"


async def fetch_exchanges(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        select u.id            as user_utterance_id,
               u.speaker_id    as user_id,
               u.text          as user_text,
               u.timestamp     as user_timestamp,
               u.status        as user_status,
               u.meta          as user_meta,
               b.id            as bot_utterance_id,
               b.text          as bot_text,
               b.status        as bot_status,
               b.error         as bot_error,
               b.attempts      as bot_attempts,
               b.meta          as bot_meta
        from utterances u
        left join utterances b on b.reply_to_id = u.id
        where u.speaker_id like 'memtest-%'
        order by u.speaker_id, u.timestamp
        """
    )


def unpack(record: asyncpg.Record) -> dict[str, object]:
    user_meta = json.loads(record["user_meta"] or "{}")
    bot_meta = json.loads(record["bot_meta"] or "{}")
    snapshot = bot_meta.get("texet_generation") or {}
    return {
        "user_id": record["user_id"],
        "persona": user_meta.get("memtest_persona", ""),
        "turn": user_meta.get("memtest_turn", ""),
        "day_number": user_meta.get("day_number", ""),
        "user_local_time": user_meta.get("user_local_time", ""),
        "is_probe": bool(user_meta.get("memtest_probe")),
        "user_text": record["user_text"],
        "user_status": record["user_status"],
        "user_timestamp": record["user_timestamp"],
        "bot_text": record["bot_text"],
        "bot_status": record["bot_status"],
        "bot_error": record["bot_error"],
        "attempts": record["bot_attempts"],
        "user_utterance_id": record["user_utterance_id"],
        "bot_utterance_id": record["bot_utterance_id"],
        "provider": snapshot.get("provider", ""),
        "model_id": snapshot.get("model_id", ""),
        "system_prompt": snapshot.get("system_prompt", ""),
        "history": snapshot.get("chat_history", []),
        "moderation_source": bot_meta.get("texet_moderation_source", ""),
        "moderation_category": bot_meta.get("texet_moderation_category", ""),
        "moderation_score": bot_meta.get("texet_moderation_score", ""),
        "user_moderation_category": user_meta.get("texet_moderation_category", ""),
    }


EXCHANGE_HEADERS = [
    "Workbook row",
    "Persona",
    "Persona description",
    "Turn",
    "Study day",
    "User local time sent",
    "Participant message",
    "Bot reply",
    "Turns of history supplied",
    "Chat history supplied to the model",
    "System prompt supplied to the model",
    "Provider",
    "Model",
    "Reply status",
    "User utt. moderated as",
    "Reply moderated as",
    "Moderation score",
    "Attempts",
    "Error",
    "User utterance id",
    "Bot utterance id",
    "Stored timestamp (UTC)",
]
EXCHANGE_WIDTHS = [
    13,
    9,
    34,
    6,
    10,
    24,
    62,
    62,
    12,
    70,
    70,
    11,
    34,
    11,
    18,
    18,
    14,
    9,
    24,
    34,
    34,
    24,
]


def exchange_row(item: dict[str, object], workbook_row: object) -> list[object]:
    persona = BY_KEY.get(str(item["persona"]))
    return [
        workbook_row,
        item["persona"],
        persona.label if persona else "",
        item["turn"],
        item["day_number"],
        item["user_local_time"],
        item["user_text"],
        item["bot_text"],
        len(item["history"]),  # type: ignore[arg-type]
        render_history(item["history"]),  # type: ignore[arg-type]
        item["system_prompt"],
        item["provider"],
        item["model_id"],
        item["bot_status"],
        item["user_moderation_category"],
        item["moderation_category"],
        item["moderation_score"],
        item["attempts"],
        item["bot_error"],
        item["user_utterance_id"],
        item["bot_utterance_id"],
        str(item["user_timestamp"]),
    ]


async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    assert pool is not None
    try:
        records = [unpack(record) for record in await fetch_exchanges(pool)]
        summaries = await pool.fetch(
            "select user_id, week_start, summary from weekly_summaries order by user_id"
        )
        system_prompt_row = await pool.fetchrow(
            "select provider, model_id, prompt from system_prompts order by created_at desc limit 1"
        )
        daily = await pool.fetch(
            "select day_number, content from daily_prompts where day_number = any($1::int[])"
            " order by day_number",
            [*STUDY_DAYS, PROBE_DAY],
        )
    finally:
        await pool.close()

    rows = load_rows()
    row_of = {
        (persona.key, turn): persona.first_row + turn - 1
        for persona in PERSONAS
        for turn in range(1, 6)
    }

    # Classify by participant id, not by history length: Run B's first turn
    # also has an empty history, and conflating the two would silently move
    # ten rows into the wrong run.
    exchanges = [item for item in records if not item["is_probe"] and item["bot_utterance_id"]]
    run_a = [item for item in exchanges if str(item["user_id"]).startswith("memtest-iso-")]
    run_b = [item for item in exchanges if str(item["user_id"]).startswith("memtest-conv-")]
    run_d = [item for item in records if item["is_probe"]]

    book = Workbook()
    book.remove(book.active)

    # ── Method ────────────────────────────────────────────────────────────
    git_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    method_rows = [
        ["Generated", datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds") + " UTC"],
        ["Code revision", git_sha],
        [
            "Reply provider / model",
            f"{system_prompt_row['provider']} / {system_prompt_row['model_id']}",
        ],
        [
            "Summary provider / model",
            "openai / gpt-4o-mini (the summariser does not take the "
            "system prompt's provider; it uses _generate_reply's defaults)",
        ],
        ["Moderation", "openai / omni-moderation-latest, live"],
        [
            "Environment",
            "Local stack running production's system prompt and daily prompts, pulled "
            "from the bot-prod database. instruction_templates and summarization_prompts "
            "are empty in production, so both runs use the code defaults, as production "
            "does. No writes were made to production.",
        ],
        [
            "Run A",
            "50 utterances, one throwaway participant each, so every reply is generated with "
            "an empty history. Same day number and local-time metadata as Run B.",
        ],
        [
            "Run B",
            "10 participants x 5 turns, strictly sequential; each turn waits for the previous "
            "reply to reach status=sent before being sent, because only sent replies enter "
            "the history.",
        ],
        [
            "Timeline",
            "Threads were authored across Sun 9 - Thu 13 Aug (study days 23-27) so each "
            "generation saw a genuine multi-day thread, then shifted back 7 days into the "
            "week of 2 Aug. That is the week the summariser reads and the week the history "
            "window excludes.",
        ],
        [
            "Run C",
            f"One weekly summary per Run B participant for week_start {TARGET_WEEK_START}, "
            "generated by the app's own summariser, one participant at a time.",
        ],
        [
            "Run D",
            f"Day {PROBE_DAY} probe sent in the current week. The thread is in the previous "
            "week, so the model receives zero turns of history and only the weekly summary. "
            f"Identical probe for all ten: “{PROBE_TEXT}”",
        ],
        [
            "Latency",
            "Not captured. The pipeline records no reply-completion time, and the stored "
            "timestamps were deliberately rewritten to build the timeline.",
        ],
        [
            "Day mapping",
            ", ".join(
                f"turn {turn} = study day {day} = {date.isoformat()} (shifted to "
                f"{(date - datetime.timedelta(days=7)).isoformat()})"
                for turn, (day, date) in enumerate(
                    zip(STUDY_DAYS, LIVE_DATES, strict=True), start=1
                )
            ),
        ],
    ]
    write_sheet(book, "Method", ["Field", "Value"], method_rows, [26, 120])

    # ── Runs ──────────────────────────────────────────────────────────────
    write_sheet(
        book,
        "Run A - isolated",
        EXCHANGE_HEADERS,
        [
            exchange_row(item, row_of.get((str(item["persona"]), item["turn"]), ""))
            for item in run_a
        ],
        EXCHANGE_WIDTHS,
    )
    write_sheet(
        book,
        "Run B - conversation",
        EXCHANGE_HEADERS,
        [
            exchange_row(item, row_of.get((str(item["persona"]), item["turn"]), ""))
            for item in run_b
        ],
        EXCHANGE_WIDTHS,
    )

    # ── Comparison ────────────────────────────────────────────────────────
    a_by_key = {(str(item["persona"]), item["turn"]): item for item in run_a}
    b_by_key = {(str(item["persona"]), item["turn"]): item for item in run_b}
    comparison = []
    for persona in PERSONAS:
        for turn in range(1, 6):
            key = (persona.key, turn)
            a_item, b_item = a_by_key.get(key), b_by_key.get(key)
            comparison.append(
                [
                    persona.first_row + turn - 1,
                    persona.key,
                    persona.label,
                    turn,
                    STUDY_DAYS[turn - 1],
                    rows[persona.first_row + turn - 1],
                    a_item["bot_text"] if a_item else "",
                    b_item["bot_text"] if b_item else "",
                    len(b_item["history"]) if b_item else 0,  # type: ignore[arg-type]
                    persona.planted,
                ]
            )
    write_sheet(
        book,
        "A vs B",
        [
            "Workbook row",
            "Persona",
            "Persona description",
            "Turn",
            "Study day",
            "Participant message",
            "Reply with NO history (Run A)",
            "Reply WITH history (Run B)",
            "Turns of history",
            "What this thread plants",
        ],
        comparison,
        [13, 9, 34, 6, 10, 62, 62, 62, 12, 46],
    )

    # ── Summaries ─────────────────────────────────────────────────────────
    transcripts: dict[str, str] = {}
    for persona in PERSONAS:
        lines = []
        for item in sorted(
            (i for i in run_b if i["persona"] == persona.key),
            key=lambda i: i["turn"],  # type: ignore[arg-type,return-value]
        ):
            lines.append(f"user: {item['user_text']}")
            lines.append(f"bot: {item['bot_text']}")
        transcripts[persona.conv_user_id] = "\n".join(lines)

    summary_rows = [
        [
            BY_KEY[record["user_id"].rsplit("-", 1)[-1]].key,
            BY_KEY[record["user_id"].rsplit("-", 1)[-1]].label,
            record["user_id"],
            str(record["week_start"]),
            record["summary"],
            len(record["summary"]),
            transcripts.get(record["user_id"], ""),
            BY_KEY[record["user_id"].rsplit("-", 1)[-1]].planted,
        ]
        for record in summaries
    ]
    write_sheet(
        book,
        "Run C - summaries",
        [
            "Persona",
            "Persona description",
            "Participant",
            "Week start",
            "Weekly summary",
            "Summary length",
            "Transcript summarised",
            "What this thread plants",
        ],
        summary_rows,
        [9, 34, 24, 12, 90, 14, 90, 46],
    )

    # ── Probe ─────────────────────────────────────────────────────────────
    summary_by_user = {record["user_id"]: record["summary"] for record in summaries}
    probe_rows = []
    for item in sorted(run_d, key=lambda i: str(i["persona"])):
        persona = BY_KEY[str(item["persona"])]
        probe_rows.append(
            [
                persona.key,
                persona.label,
                PROBE_DAY,
                item["user_text"],
                item["bot_text"],
                len(item["history"]),  # type: ignore[arg-type]
                summary_by_user.get(persona.conv_user_id, ""),
                item["system_prompt"],
                item["bot_status"],
                persona.planted,
            ]
        )
    write_sheet(
        book,
        "Run D - summary recall",
        [
            "Persona",
            "Persona description",
            "Study day",
            "Probe message",
            "Bot reply",
            "Turns of history supplied",
            "Summary the model was given",
            "System prompt supplied to the model",
            "Status",
            "What this thread plants",
        ],
        probe_rows,
        [9, 34, 10, 52, 70, 12, 90, 70, 10, 46],
    )

    # ── Config ────────────────────────────────────────────────────────────
    config_rows = [["Base system prompt (from prod)", system_prompt_row["prompt"]]]
    config_rows += [
        [f"Daily prompt - day {record['day_number']}", record["content"]] for record in daily
    ]
    write_sheet(book, "Prompts used", ["Item", "Content"], config_rows, [30, 130])

    book.save(OUT_PATH)
    print(
        f"Wrote {OUT_PATH}: Run A {len(run_a)}, Run B {len(run_b)}, "
        f"summaries {len(summary_rows)}, probes {len(probe_rows)}."
    )


if __name__ == "__main__":
    asyncio.run(main())
