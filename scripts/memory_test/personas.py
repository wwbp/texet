"""Test cohort for the SMART-R memory test.

The workbook holds 50 utterances, but they are ten five-turn threads, not fifty
independent messages. Row ranges, timezones and study days live here; the text
itself is always read from the workbook so the inputs stay authoritative.

Timeline. The threads are authored in the *current* study week so that each
generation sees a real multi-day thread ([Sunday, August 9] ... [Thursday,
August 13]) inside the since-Sunday history window. Once every thread is
complete the whole cohort is shifted back seven days, which puts it in the
previous week — where the weekly summariser looks, and where it is invisible to
the history window. The day-28 probe then runs in the current week against
nothing but the summary, which is exactly the production path.
"""

from __future__ import annotations

import datetime
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = REPO_ROOT / "SMART_R_Chatbot Memory Test v1.xlsx"

# Study days for the five turns, and the day the recall probe lands on.
# 23-27 keeps every generation's [Today's Activity] section populated and puts
# the probe on day 28 (Weekly Reflection), the last day with a prompt.
STUDY_DAYS = (23, 24, 25, 26, 27)
PROBE_DAY = 28

# Local dates the threads are authored on (Sunday -> Thursday of this week),
# and the offset applied afterwards to move them into the previous week.
LIVE_DATES = (
    datetime.date(2026, 8, 9),
    datetime.date(2026, 8, 10),
    datetime.date(2026, 8, 11),
    datetime.date(2026, 8, 12),
    datetime.date(2026, 8, 13),
)
SHIFT = datetime.timedelta(days=7)
TARGET_WEEK_START = datetime.date(2026, 8, 2)
LOCAL_SEND_TIME = datetime.time(19, 30)

# One probe for all ten threads. Holding the probe constant leaves the summary
# as the only variable, and it puts the system prompt's "never claim you cannot
# remember" rule directly under test.
PROBE_TEXT = (
    "It's been about a week since we talked. Before I catch you up, "
    "what do you remember about what's been going on with me?"
)

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class Persona:
    key: str
    label: str
    first_row: int  # 1-indexed row in the workbook; the thread is 5 rows
    utc_offset: str  # from the location the participant states, else Eastern
    planted: str  # what this thread is designed to test

    @property
    def rows(self) -> tuple[int, ...]:
        return tuple(range(self.first_row, self.first_row + 5))

    @property
    def conv_user_id(self) -> str:
        return f"memtest-conv-{self.key}"

    def iso_user_id(self, turn: int) -> str:
        return f"memtest-iso-{self.key}-t{turn}"

    @property
    def offset_delta(self) -> datetime.timedelta:
        sign = -1 if self.utc_offset.startswith("-") else 1
        hours, minutes = (int(part) for part in self.utc_offset[1:].split(":"))
        return sign * datetime.timedelta(hours=hours, minutes=minutes)


PERSONAS: tuple[Persona, ...] = (
    Persona(
        "p01",
        "Alcohol recovery, church, wife and three daughters",
        1,
        "-04:00",
        "20 years sober; therapy added mid-thread; youngest daughter leaving for college",
    ),
    Persona(
        "p02",
        "ED night-shift nurse",
        6,
        "-04:00",
        "brevity preference stated three times; application -> accepted -> starting",
    ),
    Persona(
        "p03",
        "Living with HIV, Atlanta, dog Bailey",
        11,
        "-04:00",
        "regimen switched to single tablet; engagement -> spring wedding",
    ),
    Persona(
        "p04",
        "Opioid recovery on buprenorphine",
        16,
        "-05:00",
        "lapse disclosed at turn 3 then three weeks clean; meetings 2/wk -> 3/wk",
    ),
    Persona(
        "p05",
        "Rheumatoid arthritis and depression, cat Winston",
        21,
        "-04:00",
        "Winston dies at turn 3; decision to move near son at turn 5",
    ),
    Persona(
        "p06",
        "MPH student, rural treatment access",
        26,
        "-04:00",
        "capstone -> defended -> county job accepted -> graduation",
    ),
    Persona(
        "p07",
        "Type 2 diabetes, Phoenix",
        31,
        "-07:00",
        "no-pep-talk preference stated three times; son leaving for Tucson",
    ),
    Persona(
        "p08",
        "Generalized anxiety, design student, cat Juniper",
        36,
        "-04:00",
        "escitalopram started at turn 3; text-not-calls preference",
    ),
    Persona(
        "p09",
        "Bipolar disorder and alcohol, bus driver",
        41,
        "-05:00",
        "street address disclosed at turn 1; mood-stabiliser change; dispatcher application",
    ),
    Persona(
        "p10",
        "Chronic back pain, cannabis, dog Otis",
        46,
        "-04:00",
        "Otis dies at turn 4; retirement and move to North Carolina",
    ),
)


def load_rows() -> dict[int, str]:
    """Read column B of the workbook, keyed by the row number in column A."""
    with zipfile.ZipFile(WORKBOOK) as archive:
        strings_xml = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(node.text or "" for node in item.iter(f"{_NS}t"))
            for item in strings_xml.findall(f"{_NS}si")
        ]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows: dict[int, str] = {}
    for row in sheet.iter(f"{_NS}row"):
        cells: dict[str, str] = {}
        for cell in row.findall(f"{_NS}c"):
            value = cell.find(f"{_NS}v")
            if value is None or value.text is None:
                continue
            column = "".join(ch for ch in (cell.get("r") or "") if ch.isalpha())
            cells[column] = shared[int(value.text)] if cell.get("t") == "s" else value.text
        if "A" in cells and "B" in cells:
            rows[int(cells["A"])] = cells["B"]
    return rows


def local_datetime(date: datetime.date, offset: datetime.timedelta) -> datetime.datetime:
    return datetime.datetime.combine(date, LOCAL_SEND_TIME, tzinfo=datetime.timezone(offset))


def turn_metadata(persona: Persona, turn: int) -> dict[str, object]:
    """The metadata the hub would send for this turn (1-indexed)."""
    when = local_datetime(LIVE_DATES[turn - 1], persona.offset_delta)
    return {
        "source": "memory-test",
        "day_number": STUDY_DAYS[turn - 1],
        "is_initial": False,
        "user_local_time": when.isoformat(),
        "memtest_persona": persona.key,
        "memtest_turn": turn,
    }


def probe_metadata(persona: Persona) -> dict[str, object]:
    now_local = datetime.datetime.now(datetime.timezone(persona.offset_delta))
    return {
        "source": "memory-test",
        "day_number": PROBE_DAY,
        "is_initial": False,
        "user_local_time": now_local.replace(microsecond=0).isoformat(),
        "memtest_persona": persona.key,
        "memtest_turn": 6,
        "memtest_probe": True,
    }
