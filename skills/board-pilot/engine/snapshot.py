"""Board snapshot persistence — atomic, so a crashed/wiped snapshot can never
cause a mass re-arm.

The durable program-counter is the board's `pipeline` field (see interfaces.py),
so arming is gated on `pipeline is None`, not on this snapshot. The engine does
NOT read this snapshot back to arm (arming is level-triggered on the pipeline
marker), so a wiped or stale snapshot is harmless — it is kept only as a
diagnostic record of the last-seen board, always written via
write-temp-then-rename so a half-written file is impossible.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def load(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_atomic(path, status_by_id: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".snap-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status_by_id, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
