from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


_load_env_file(Path(__file__).resolve().parents[1] / ".env.db")
