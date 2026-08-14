#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared multi-writer-safe registry mechanics: advisory lock + atomic replace.

Pure stdlib primitives factored out of `scripts/workspace_registry.py` so a
second machine-global registry (`scripts/capability_registry.py`) can follow
the SAME protocol shape — take an advisory lock, read the whole file, modify
in memory, atomic-replace, release the lock — documented at
`skills/workspace/references/model.md` § "The multi-writer protocol", without
copy-pasting those mechanics a second time.

This module carries NO registry-specific schema knowledge (no field names, no
version ceiling, no entry shape) — each caller owns its own file format and
version policy and composes these primitives around it. `workspace_registry.py`
and `capability_registry.py` both import from here; neither imports the other.
"""

import os
import tempfile
from datetime import datetime, timezone

try:
    # POSIX advisory locking. On a platform WITHOUT fcntl (non-POSIX) the lock
    # file is still created but NOT actually held — the whole read-modify-write
    # then runs UNSERIALIZED there (advisory best-effort only).
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


def now_iso() -> str:
    """ISO-8601 UTC with a trailing Z (matches the schema's timestamp form)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coerce_version(version) -> int | None:
    """Coerce an on-disk `version` field to int, or None if it is not numeric.

    Accepts an int (kept as-is), a decimal string matching `^\\d+$` (`"2"` → 2),
    and an integral float (`2.0` → 2) — the JSON/YAML representations that all
    mean the same schema version. A bool, a non-integral float, a non-numeric
    string, or a missing/other type is NON-coercible (None); the caller then
    fails closed rather than guessing a version for it.
    """
    if isinstance(version, bool):
        return None  # bool is an int subclass, but true/false is not a version
    if isinstance(version, int):
        return version
    if isinstance(version, float):
        return int(version) if version.is_integer() else None
    if isinstance(version, str) and version.isdigit():
        return int(version)
    return None


class AdvisoryLock:
    """POSIX advisory `flock` on a dedicated lock file — best-effort elsewhere.

    `with AdvisoryLock(lock_path):` blocks until any other cooperating writer
    releases the SAME lock file, then runs the read-modify-write protocol
    inside it. The lock is advisory: it coordinates writers that take it, and
    cannot stop a writer that bypasses it (see `docs/workspaces.md` § Known
    limitations for what that means in practice). On a platform without
    `fcntl` the lock file is still created but not actually held — the
    critical section then runs unserialized (documented best-effort only).
    """

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self.fd = -1

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        if fcntl is not None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
        return False


def atomic_write_bytes(dest: str, data: bytes, tmp_path: str | None = None) -> None:
    """Write `data` to `dest` atomically: temp file in the same dir + `os.replace`.

    A failure before the rename leaves `dest` exactly as it was (no torn
    write) — the `finally` unlink means a stray temp file never survives a
    failed attempt either. Pass a fixed `tmp_path` for a documented, stable
    temp name (safe under a lock — one writer at a time, e.g. the registry's
    own `<name>.tmp`); omit it for a random `tempfile.mkstemp` name (used for
    one-off writes outside the primary registry file, e.g. a rotated backup).
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if tmp_path:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        tmp = tmp_path
    else:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".regio-",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
