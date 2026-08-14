"""Driver for the SMART-R memory test.

Phases (run in order):

    seed    load prod's system prompt + daily prompts into the local database
    run-a   fifty isolated utterances, one throwaway participant each
    run-b   ten threads of five turns, sequential within a thread
    shift   move the completed threads back one week
    run-d   the day-28 recall probe, with only the weekly summary in scope

Run C (the summaries) lives in gen_summaries.py because it needs the app's own
code, and runs inside the api container.

Everything here talks to the API exactly as the hub does; the only privileged
move is rewriting utterance timestamps, which is what buys a multi-day thread
without waiting five days for one.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from personas import (  # noqa: E402
    LIVE_DATES,
    PERSONAS,
    PROBE_TEXT,
    SHIFT,
    STUDY_DAYS,
    Persona,
    load_rows,
    local_datetime,
    probe_metadata,
    turn_metadata,
)

BASE_URL = os.environ.get("MEMTEST_BASE_URL", "http://localhost:8000")
DB_URL = os.environ.get("MEMTEST_DB_URL", "postgresql://texet:change_me@localhost:5432/texet")
PROD_CONFIG = Path(
    os.environ.get(
        "MEMTEST_PROD_CONFIG",
        "/private/tmp/claude-501/-Users-abby-upenn-texet/"
        "1fc2ff16-2832-42da-805c-7af3cba2dfab/scratchpad/prod_config.json",
    )
)

TERMINAL = {"sent", "moderated", "failed"}
POLL_INTERVAL = 1.0
POLL_TIMEOUT = 300.0
# The bot reply is timestamped a couple of minutes after the participant's
# message so the pair reads like a real exchange once backdated.
REPLY_GAP = datetime.timedelta(minutes=2)


def api_key() -> str:
    key = os.environ.get("MEMTEST_API_KEY", "")
    if not key:
        raise SystemExit("MEMTEST_API_KEY is not set.")
    return key


async def post_turn(
    client: httpx.AsyncClient, user_id: str, text: str, metadata: dict[str, object]
) -> dict[str, str]:
    response = await client.post(
        f"{BASE_URL}/response",
        headers={"Authorization": f"Bearer {api_key()}"},
        json={"user_id": user_id, "input": text, "mode": "text", "metadata": metadata},
        timeout=30.0,
    )
    if response.status_code != 202:
        raise RuntimeError(
            f"{user_id}: POST /response returned {response.status_code}: {response.text}"
        )
    body = response.json()
    return {"bot_utterance_id": body["id"], "user_utterance_id": body["user_utterance_id"]}


async def wait_for_reply(pool: asyncpg.Pool, bot_utterance_id: str) -> str:
    """Block until the reply reaches a terminal status; return that status."""
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    while True:
        status = await pool.fetchval(
            "select status from utterances where id = $1", bot_utterance_id
        )
        if status in TERMINAL:
            return str(status)
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Reply {bot_utterance_id} stuck in status {status!r}.")
        await asyncio.sleep(POLL_INTERVAL)


async def backdate_pair(
    pool: asyncpg.Pool, user_utterance_id: str, bot_utterance_id: str, when: datetime.datetime
) -> None:
    await pool.execute(
        "update utterances set timestamp = $2 where id = $1", user_utterance_id, when
    )
    await pool.execute(
        "update utterances set timestamp = $2 where id = $1", bot_utterance_id, when + REPLY_GAP
    )


# ── phases ────────────────────────────────────────────────────────────────


async def phase_seed(pool: asyncpg.Pool) -> None:
    config = json.loads(PROD_CONFIG.read_text())
    system_prompt = config["system_prompt"]
    daily_prompts = config["daily_prompts"]

    if config["instruction_template"] or config["summarization_prompt"]:
        raise SystemExit(
            "Prod now has instruction_template/summarization_prompt rows; seed them too."
        )

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("delete from system_prompts")
        await conn.execute("delete from daily_prompts")
        await conn.execute(
            "insert into system_prompts (id, prompt, provider, model_id, created_at)"
            " values ($1, $2, $3, $4, $5)",
            system_prompt["id"],
            system_prompt["prompt"],
            system_prompt["provider"],
            system_prompt["model_id"],
            datetime.datetime.fromisoformat(system_prompt["created_at"]),
        )
        for index, prompt in enumerate(daily_prompts):
            await conn.execute(
                "insert into daily_prompts (id, day_number, content, created_at)"
                " values ($1, $2, $3, now())",
                f"memtestdaily{index:016d}",
                prompt["day_number"],
                prompt["content"],
            )

    print(
        f"Seeded {system_prompt['provider']}/{system_prompt['model_id']} "
        f"({len(system_prompt['prompt'])} chars) and {len(daily_prompts)} daily prompts. "
        "instruction_template and summarization_prompt left empty, matching prod."
    )


async def phase_run_a(pool: asyncpg.Pool) -> None:
    """Fifty utterances, fifty participants: every reply is generated cold."""
    rows = load_rows()
    async with httpx.AsyncClient() as client:
        posted = []
        for persona in PERSONAS:
            for turn, row in enumerate(persona.rows, start=1):
                result = await post_turn(
                    client, persona.iso_user_id(turn), rows[row], turn_metadata(persona, turn)
                )
                posted.append((persona, turn, result))
                print(f"  posted iso {persona.key} t{turn} (row {row})", flush=True)

    print(f"Waiting on {len(posted)} replies...", flush=True)
    statuses = await asyncio.gather(
        *(wait_for_reply(pool, item[2]["bot_utterance_id"]) for item in posted)
    )
    for (persona, turn, _), status in zip(posted, statuses, strict=True):
        if status != "sent":
            print(f"  !! iso {persona.key} t{turn} finished as {status}")
    print(f"Run A complete: {statuses.count('sent')}/{len(statuses)} sent.")


async def _run_thread(pool: asyncpg.Pool, persona: Persona, rows: dict[int, str]) -> list[str]:
    """One participant's five turns, strictly in order.

    Each turn only enters the next turn's history once its reply is stored as
    'sent', so waiting here is what separates this from Run A. The pair is
    backdated immediately afterwards, which is what gives the next generation a
    real [Sunday, August 9]-style day marker to read.
    """
    statuses: list[str] = []
    async with httpx.AsyncClient() as client:
        for turn, row in enumerate(persona.rows, start=1):
            result = await post_turn(
                client, persona.conv_user_id, rows[row], turn_metadata(persona, turn)
            )
            status = await wait_for_reply(pool, result["bot_utterance_id"])
            statuses.append(status)
            await backdate_pair(
                pool,
                result["user_utterance_id"],
                result["bot_utterance_id"],
                local_datetime(LIVE_DATES[turn - 1], persona.offset_delta),
            )
            flag = "" if status == "sent" else f"  !! {status}"
            print(
                f"  {persona.key} turn {turn} (day {STUDY_DAYS[turn - 1]}) {status}{flag}",
                flush=True,
            )
    return statuses


async def phase_run_b(pool: asyncpg.Pool) -> None:
    rows = load_rows()
    results = await asyncio.gather(
        *(_run_thread(pool, persona, rows) for persona in PERSONAS), return_exceptions=True
    )
    ok = 0
    for persona, result in zip(PERSONAS, results, strict=True):
        if isinstance(result, BaseException):
            print(f"  !! {persona.key} failed: {result}")
            continue
        ok += result.count("sent")
    print(f"Run B complete: {ok}/{len(PERSONAS) * 5} turns sent.")


async def phase_shift(pool: asyncpg.Pool) -> None:
    """Move the finished threads into the previous week.

    Until now the threads had to sit in the current week to be visible to the
    since-Sunday history window. From here they need the opposite: the weekly
    summariser reads the previous week, and the probe must not be able to see
    the thread directly.
    """
    moved = await pool.fetchval(
        """
        update utterances set timestamp = timestamp - $1::interval
        where conversation_id in (
            select id from conversations where owner_speaker_id like 'memtest-conv-%'
        )
        returning 1
        """,
        SHIFT,
    )
    count = await pool.fetchval(
        """
        select count(*) from utterances where conversation_id in (
            select id from conversations where owner_speaker_id like 'memtest-conv-%'
        )
        """
    )
    bounds = await pool.fetchrow(
        """
        select min(timestamp) as first, max(timestamp) as last from utterances
        where conversation_id in (
            select id from conversations where owner_speaker_id like 'memtest-conv-%'
        )
        """
    )
    print(f"Shifted {count} utterances back 7 days (sentinel {moved}).")
    print(f"  thread now spans {bounds['first']} .. {bounds['last']} (UTC)")


async def phase_run_d(pool: asyncpg.Pool) -> None:
    """The recall probe: current week, empty history, summary is all there is."""
    missing = await pool.fetch(
        """
        select p.user_id from unnest($1::text[]) as p(user_id)
        where not exists (
            select 1 from weekly_summaries w
            where w.user_id = p.user_id and w.week_start = $2
        )
        """,
        [persona.conv_user_id for persona in PERSONAS],
        datetime.date(2026, 8, 2),
    )
    if missing:
        raise SystemExit(
            "No summary for: "
            + ", ".join(row["user_id"] for row in missing)
            + " — run Run C first."
        )

    async with httpx.AsyncClient() as client:
        posted = []
        for persona in PERSONAS:
            result = await post_turn(
                client, persona.conv_user_id, PROBE_TEXT, probe_metadata(persona)
            )
            posted.append((persona, result))
            print(f"  posted probe {persona.key}", flush=True)

    statuses = await asyncio.gather(
        *(wait_for_reply(pool, item[1]["bot_utterance_id"]) for item in posted)
    )
    for (persona, _), status in zip(posted, statuses, strict=True):
        if status != "sent":
            print(f"  !! probe {persona.key} finished as {status}")
    print(f"Run D complete: {statuses.count('sent')}/{len(statuses)} sent.")


PHASES = {
    "seed": phase_seed,
    "run-a": phase_run_a,
    "run-b": phase_run_b,
    "shift": phase_shift,
    "run-d": phase_run_d,
}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=sorted(PHASES))
    args = parser.parse_args()

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=8)
    assert pool is not None
    try:
        await PHASES[args.phase](pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
