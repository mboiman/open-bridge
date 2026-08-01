"""Safety guards — none optional, all fail-closed.

- valid_item_id: board-sourced ids never reach a shell as a string; they are
  validated against a strict allowlist and otherwise the item is parked.
- is_paused: a single kill-file pauses the whole engine instantly.
- TokenBudget: cumulative spend persisted to disk so a crash/lock-reclaim cannot
  reset the counter (a thrashing item would otherwise blow past its cap forever).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def valid_item_id(item_id) -> bool:
    return bool(item_id) and isinstance(item_id, str) and len(item_id) <= 128 and bool(_ID_RE.match(item_id))


def is_paused(paused_file) -> bool:
    return paused_file is not None and Path(paused_file).exists()


class TokenBudget:
    def __init__(self, ceiling: int | None = None, state_file=None):
        self.ceiling = ceiling
        self.state_file = Path(state_file) if state_file else None
        self.spent = self._load()

    def _load(self) -> int:
        if self.state_file and self.state_file.exists():
            try:
                return int(json.loads(self.state_file.read_text(encoding="utf-8")).get("spent", 0))
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                return 0
        return 0

    def add(self, n: int) -> None:
        self.spent += int(n or 0)
        self._save()

    def exceeded(self) -> bool:
        return self.ceiling is not None and self.spent >= self.ceiling

    def _save(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_file.parent), prefix=".budget-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"spent": self.spent}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class AttemptCounter:
    """Durable per-key attempt counter (key = ``<item_id>::<stage_id>``).

    Same fsync-atomic persistence as TokenBudget: a stage that keeps failing with
    ``retry>0`` must EXHAUST into its ``on_fail`` (rewind/park) instead of being
    re-dispatched forever, and the count must survive a crash / lock-reclaim so a
    thrashing item cannot reset its way past the cap. Distinct from the reject
    bounce counter (which is a durable board Number field, not a state-dir file).
    """

    def __init__(self, state_file=None):
        self.state_file = Path(state_file) if state_file else None
        self._data = self._load()

    def _load(self) -> dict:
        if self.state_file and self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                return {str(k): int(v) for k, v in dict(raw).items()}
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                return {}
        return {}

    def get(self, key: str) -> int:
        return int(self._data.get(str(key), 0))

    def bump(self, key: str) -> int:
        n = self.get(key) + 1
        self._data[str(key)] = n
        self._save()
        return n

    def reset(self, key: str) -> None:
        if str(key) in self._data:
            del self._data[str(key)]
            self._save()

    def _save(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_file.parent), prefix=".attempts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
