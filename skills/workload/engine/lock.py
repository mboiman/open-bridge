"""Never start what is still running, applied to the control plane.

The run plane keeps a workload from overlapping with itself; this keeps two
sessions from provisioning the same id at the same time. Both are the same rule:
a second start while the first is still going loses work silently.

The exclusion is a KERNEL lock (``flock``) on a file, not a decision made by
reading one. Reading a pid and then writing one is two steps, and two sessions
can both pass the read: at a barrier, eight of eight processes held the same
"lock" at once. ``flock`` decides in one step, and it is released by the kernel
when the holder dies, so a crashed session cannot block the next one forever and
no stale-pid reclaim is needed to make that true.

The file also carries the holder's pid, in plain text. That is information for a
human reading the refusal, never the mechanism.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from engine import errors

LOCK_DIR = Path(".bridge") / "workload-locks"


def _read_pid(path: Path):
    """Whoever last wrote into this lock file, for the message. Never the gate."""
    try:
        return int(path.read_text(encoding="utf-8").strip().splitlines()[0])
    except (FileNotFoundError, ValueError, IndexError, OSError):
        return None


@contextmanager
def workload_lock(root, workload_id: str):
    """Hold the lock for one workload, and release it however the body ends."""
    path = Path(root) / LOCK_DIR / f"{workload_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)

    # O_RDWR, never O_TRUNC: truncating before the lock is taken would wipe the
    # holder's pid out of the file the refusal is supposed to name.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise errors.LockHeld(pid=_read_pid(path) or 0, workload=workload_id,
                                  lock=str(path)) from None
    except BaseException:
        os.close(fd)
        raise

    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        # The file stays. Unlinking it would let a second session open a fresh
        # inode and lock that instead, which is two holders wearing one name.
        os.close(fd)
