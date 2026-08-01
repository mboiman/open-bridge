"""Per-item lock — prevents a 1-minute poll from double-dispatching a long job.

A lock is a small JSON file carrying the worker pid + a heartbeat timestamp.
Liveness uses `kill -0` (process alive) as the PRIMARY signal; a lock is only
reclaimed when the holder is BOTH heartbeat-stale AND pid-dead — so a busy
16 GB box that briefly swap-starves a worker does not trigger a false reclaim.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


class Lock:
    def __init__(self, lockdir, key, stale_seconds: int = 900):
        self.path = Path(lockdir) / f"{key}.lock"
        self.stale_seconds = stale_seconds

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def held_by_alive(self, now: float | None = None) -> bool:
        info = self._read()
        if not info:
            return False
        now = time.time() if now is None else now
        alive = _pid_alive(info.get("pid"))
        fresh = (now - float(info.get("heartbeat", 0))) < self.stale_seconds
        # Reclaim ONLY when the holder is both gone AND silent. A lock stays held
        # while its owner is still alive OR still heartbeating — kill -0 is the
        # primary signal, so a live-but-swap-starved worker keeps its lock.
        return alive or fresh

    def acquire(self, pid: int | None = None, now: float | None = None) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = {
            "pid": int(pid) if pid is not None else os.getpid(),
            "heartbeat": float(now) if now is not None else time.time(),
        }
        data = json.dumps(info)
        try:
            # Atomic create — wins the race against an overlapping tick (no TOCTOU).
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            return True
        except FileExistsError:
            if self.held_by_alive(now=now):
                return False
            # Holder is gone AND silent → steal via atomic replace.
            tmp = self.path.with_name(f"{self.path.name}.{info['pid']}.tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.path)
            return True

    def heartbeat(self, now: float | None = None) -> None:
        info: dict = self._read() or {"pid": os.getpid()}
        info["heartbeat"] = float(now) if now is not None else time.time()
        self.path.write_text(json.dumps(info), encoding="utf-8")

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
