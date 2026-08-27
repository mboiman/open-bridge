"""The only subprocess path in the skill, and the home of two hard rules.

Rule 1: every outbound call carries a deadline, and an expired deadline is a
REPORTED error. It is never a synthetic return code such as 124, because a code
is a value somebody can ignore and a hang that returns 124 looks like a run.

Rule 2: the deadline kills the process GROUP, not the direct child.
``subprocess.run(timeout=...)`` raises and kills the child, and a grandchild that
inherited the output pipe carries on holding it, so the cleanup then waits for an
EOF that never comes. Every call therefore starts its own session
(``start_new_session=True``) and the expiry escalates over ``os.killpg``.

Output is drained by two reader threads rather than by ``communicate()``, which
is what makes the partial output of a killed call survivable: the bytes written
before the deadline are already in hand when the group dies.

Nothing here knows a host name, a path or a uid. The uid is discovered on the
machine by :func:`probe_context`, because a guessed one points a whole plan at
the wrong domain.
"""

from __future__ import annotations

import math
import os
import shlex
import signal
import string
import subprocess
import threading
import time
from dataclasses import dataclass

from engine import errors
from engine import model

#: Fallbacks for callers that carry no config. Config always wins when present.
DEFAULT_STEP_TIMEOUT_SEC = 60
DEFAULT_CONNECT_TIMEOUT_SEC = 10

#: How long a killed group may take to disappear before the escalation, and how
#: long the reader threads are given to hand over what they already read.
_GRACE_SEC = 2.0
_DRAIN_SEC = 2.0

#: Characters that need no quoting in a POSIX shell word. Everything else is
#: backslash escaped, which keeps a semicolon a semicolon instead of a command.
_SAFE = frozenset(string.ascii_letters + string.digits + "@%+=:,./-_")

#: One cheap read only call. Three lines out: uid, home, zone. Each of the three
#: is a fact this skill must never guess.
CONTEXT_PROBE = (
    'id -u; echo "$HOME"; '
    "readlink /etc/localtime 2>/dev/null | sed -e 's#.*/zoneinfo/##'"
)


@dataclass(frozen=True)
class Completed:
    """What a finished call left behind. A deadline never produces one."""

    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    argv: tuple = ()
    duration_sec: float = 0.0


# ── quoting ──────────────────────────────────────────────────────────────────

def sh_quote(word) -> str:
    """Quote one word for a POSIX shell.

    Backslash escaping rather than wrapping in single quotes: the wrapped form
    leaves a payload like ``two words; rm -rf /`` literally readable inside the
    command line, and a reviewer cannot tell a quoted semicolon from a live one.
    """
    word = str(word)
    if word == "":
        return "''"
    if "\n" in word:  # a backslash before a newline is a line continuation
        return shlex.quote(word)
    if all(ch in _SAFE for ch in word):
        return word
    return "".join(ch if ch in _SAFE else "\\" + ch for ch in word)


def sh_join(argv) -> str:
    """One shell command line out of an argv, every word quoted."""
    return " ".join(sh_quote(a) for a in argv)


# ── the load bearing function ────────────────────────────────────────────────

def _drain(pipe, sink) -> None:
    """Read a pipe to EOF into a list of chunks, without blocking the caller."""
    try:
        while True:
            chunk = pipe.read1(4096)
            if not chunk:
                break
            sink.append(chunk)
    except (ValueError, OSError):  # the pipe was closed under us
        pass
    finally:
        try:
            pipe.close()
        except (ValueError, OSError):
            pass


def _text(chunks) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


def _deadline_or_refuse(argv, timeout_sec) -> float:
    """The deadline for one call, or a refusal. There is no default here.

    ``proc.wait(timeout=None)`` waits forever, so a missing deadline does not
    fail, it hangs, and a hang is the one outcome this module exists to make
    impossible. Rule 1 says EVERY outbound call carries a deadline; a caller
    that forgot one is a bug in the caller, and a fallback would hide it under
    a number nobody chose. The ssh side already refuses badly nested deadlines
    (:class:`errors.InvalidTimeout`); this is the same refusal for the deadline
    that is not there at all.
    """
    seconds = 0.0
    if isinstance(timeout_sec, (int, float)) and not isinstance(timeout_sec, bool):
        seconds = float(timeout_sec)
    # `inf` and `nan` are floats, and both walked past a `<= 0` test: `inf` is
    # larger than everything and `nan` compares False against everything. Either
    # one reached `proc.wait(timeout=...)` as a deadline that can never expire,
    # which is the hang this whole function exists to make impossible. The
    # finiteness test has to come first, or `nan` slips through it too.
    if not math.isfinite(seconds) or seconds <= 0:
        raise errors.InvalidTimeout(
            f"no usable deadline for {sh_join(argv)}: a call outwards without one "
            f"waits forever instead of failing, timeout_sec={timeout_sec!r}",
            step_timeout_sec=timeout_sec, argv=tuple(str(a) for a in argv))
    return seconds


def _wait_quiet(proc, seconds: float) -> bool:
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _kill_group(proc) -> None:
    """TERM then KILL the whole group, never only the child.

    A grandchild that survives keeps the output pipe open, and the cleanup after
    it blocks forever. That is the exact shape of a three and a half hour hang.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        if _wait_quiet(proc, _GRACE_SEC) and sig is signal.SIGKILL:
            break


def run_argv(argv, *, timeout_sec, cwd=None, env=None) -> Completed:
    """Run one argv under a hard deadline, in its own process group.

    Never a shell string, never an inherited stdin, and never without a deadline:
    a missing or non positive ``timeout_sec`` is refused before anything starts,
    because the alternative is an unbounded wait that looks like work. On expiry
    the whole group is killed and a :class:`errors.StepTimeout` is raised
    carrying whatever output had already arrived.
    """
    argv = tuple(str(a) for a in argv)
    deadline = _deadline_or_refuse(argv, timeout_sec)
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,   # an inherited stdin is how a call waits forever
        cwd=cwd,
        env=env,
        start_new_session=True,     # our own group, so there is one to kill
    )
    out: list = []
    err: list = []
    readers = (
        threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
    )
    for reader in readers:
        reader.start()

    if not _wait_quiet(proc, deadline):
        _kill_group(proc)
        for reader in readers:
            reader.join(_DRAIN_SEC)
        raise errors.StepTimeout(
            argv=argv,
            timeout_sec=timeout_sec,
            partial_stdout=_text(out),
            partial_stderr=_text(err),
        )

    for reader in readers:
        reader.join(_DRAIN_SEC)
    return Completed(
        rc=proc.returncode,
        stdout=_text(out),
        stderr=_text(err),
        argv=argv,
        duration_sec=round(time.monotonic() - started, 3),
    )


# ── steps ────────────────────────────────────────────────────────────────────

def ssh_argv(host, argv, *, connect_timeout_sec) -> list:
    """Build the remote form of an argv. Built here, executed by run_argv."""
    target = f"{host.ssh_user}@{host.ssh_host}" if getattr(host, "ssh_user", None) else host.ssh_host
    out = [
        "ssh",
        "-n",                        # an ssh that inherits stdin hangs under a service manager
        "-o", "BatchMode=yes",       # never sit at a password prompt
        "-o", f"ConnectTimeout={int(connect_timeout_sec)}",
    ]
    port = getattr(host, "ssh_port", None)
    if port:
        out += ["-p", str(port)]
    # ssh does not carry an argv. Everything after the target is joined with
    # spaces and handed to the remote LOGIN shell, which splits it again -- and
    # that shell is whatever the account happens to use, with its own globbing
    # and its own word rules. So the payload is wrapped in `/bin/sh -c` to
    # normalise it, and then quoted ONCE MORE, as a single word, for the login
    # shell that does the splitting.
    #
    # The second quoting is the whole point and was missing. Without it the
    # remote `sh -c` receives three words or five instead of two plus a script,
    # and `sh -c` takes only the FIRST as its command string: the rest become
    # positional parameters and are never run. For a step that was already a
    # shell invocation this ran a bare `/bin/sh` with stdin closed, which read
    # nothing and exited ZERO. Empty output, no error, and the host facts came
    # back as three empty strings that the plan was then built on.
    out += [str(target), "--", "/bin/sh", "-c", sh_quote(sh_join(argv))]
    return out


def run_step(step, host, *, default_timeout_sec, connect_timeout_sec=None) -> Completed:
    """Run one step, locally or over ssh, and judge its return code.

    An elevated step is never executed here. It is handed back to the caller so
    a human can run it: escalating silently on a box with live services is not
    on the table.
    """
    if step.requires_elevation:
        raise errors.ElevationRequired(step=sh_join(step.argv), purpose=step.purpose)

    timeout_sec = step.timeout_sec or default_timeout_sec
    argv = tuple(str(a) for a in step.argv)

    if not getattr(host, "is_local", False):
        connect = DEFAULT_CONNECT_TIMEOUT_SEC if connect_timeout_sec is None else connect_timeout_sec
        if connect >= timeout_sec:
            # Otherwise the outer deadline is the only one that ever fires and
            # the connect phase is unbounded in practice.
            raise errors.InvalidTimeout(
                connect_timeout_sec=connect, step_timeout_sec=timeout_sec, host=host.slug)
        argv = tuple(ssh_argv(host, argv, connect_timeout_sec=connect))

    done = run_argv(argv, timeout_sec=timeout_sec)
    expect = tuple(step.expect_rc or ())
    if expect and done.rc not in expect:
        raise errors.StepFailed(argv=argv, rc=done.rc, stderr=done.stderr, purpose=step.purpose)
    return done


def step_runner(step, host, *, timeout_sec=None, connect_timeout_sec=None) -> Completed:
    """The default runner every other module calls, and every test replaces."""
    return run_step(
        step, host,
        default_timeout_sec=timeout_sec or DEFAULT_STEP_TIMEOUT_SEC,
        connect_timeout_sec=connect_timeout_sec,
    )


# ── host facts ───────────────────────────────────────────────────────────────

def probe_context(host, cfg, *, timeout_sec, runner=None):
    """Ask the host for the facts render must not guess.

    The numeric uid especially: a service manager target is built from it, and a
    hardcoded one points the whole plan at somebody else's domain.
    """
    from engine.backends.base import RenderContext  # lazy: exec stays importable alone

    runner = runner or step_runner
    step = model.Step(
        argv=("/bin/sh", "-c", CONTEXT_PROBE),
        purpose="read uid, home and zone from the host",
        expect_rc=(),
    )
    done = runner(step, host, timeout_sec=timeout_sec)
    lines = [line.strip() for line in (done.stdout or "").splitlines()]
    lines += ["", "", ""]
    uid, home, zone = lines[0], lines[1], lines[2]

    # Read, or stop. The padding above turns a short answer into empty strings,
    # which used to travel straight into the plan: `gui/<uid>` became `gui//`,
    # every path became root anchored, and provision refused with a collision
    # against a unit that cannot exist. Nothing marked it, because the step runs
    # with no expected return code and the real failure exited zero.
    #
    # The two that are checked are the two with no fallback. `uid` must be a
    # number because `gui/<uid>` is built from it and a word there addresses no
    # domain at all; `home` must be absolute because every rendered path hangs
    # off it. The zone has a legitimate fallback in the host's own declaration
    # and is therefore left alone.
    if not uid.isdigit() or not home.startswith("/"):
        raise errors.HostFactsUnreadable(
            host=getattr(host, "slug", "?"), uid=uid or "<empty>",
            home=home or "<empty>", rc=done.rc,
            stderr=(done.stderr or "").strip() or "<empty>")

    stamp_dir = cfg.stamp_dir or ""
    if stamp_dir.startswith("~"):
        stamp_dir = home + stamp_dir[1:]
    elif stamp_dir.startswith("$HOME"):
        stamp_dir = home + stamp_dir[len("$HOME"):]

    return RenderContext(
        uid=uid,
        home=home,
        stamp_dir=stamp_dir,
        dispatcher_registry=cfg.dispatcher_registry,
        host_timezone=zone or getattr(host, "timezone", None),
    )
