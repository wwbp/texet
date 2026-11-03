"""Every model column must be exported or excluded on purpose.

Add a column without a decision in app/console/exports.py and this fails.
"""

from __future__ import annotations

from app.console.exports import EXPORT_EXCLUDED, EXPORTED
from app.models import Base


def _all_columns() -> set[str]:
    return {f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables for c in t.columns}


def _covered(column: str) -> bool:
    table = column.split(".")[0]
    return any(key in EXPORTED or key in EXPORT_EXCLUDED for key in (column, f"{table}.*"))


def test_every_column_is_exported_or_excluded() -> None:
    undecided = sorted(c for c in _all_columns() if not _covered(c))
    assert not undecided, f"Add these to EXPORTED or EXPORT_EXCLUDED: {undecided}"


def test_no_column_is_both_exported_and_excluded() -> None:
    assert not set(EXPORTED) & set(EXPORT_EXCLUDED)


def test_no_stale_entries() -> None:
    columns = _all_columns()
    tables = {c.split(".")[0] for c in columns}
    stale = [
        key
        for key in [*EXPORTED, *EXPORT_EXCLUDED]
        if key not in columns and not (key.endswith(".*") and key[:-2] in tables)
    ]
    assert not stale, f"Remove entries for columns that no longer exist: {stale}"


def test_every_entry_says_where_or_why() -> None:
    assert all(v.strip() for v in [*EXPORTED.values(), *EXPORT_EXCLUDED.values()])
