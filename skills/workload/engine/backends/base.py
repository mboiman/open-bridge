"""The contract every backend satisfies, plus the vocabulary they share.

A backend turns one declaration into the exact bytes that belong on a machine,
and into the step plans that install, replace, disable, remove, probe and
enumerate it. Everything in this package is pure: no filesystem, no subprocess,
no clock. The same declaration renders the same bytes forever, which is what
turns a second provision run into a no-op instead of a hope.

The four capability attributes (`platforms`, `kinds`, `guarantees`,
`wrappable`) are DATA. They are what lets the caller dispatch with one table
lookup and zero comparisons against a runtime or platform name.
"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from engine import errors
from engine.model import (
    MARKER_ENV_DIGEST,
    MARKER_ENV_ID,
    Step,
    ensure_env_name,
)

#: Used when no configuration says otherwise. `config.load_config` carries the
#: same default, and `build(cfg)` on each backend module overrides it.
DEFAULT_LABEL_PREFIX = "bridge"

#: Minutes in a day, for the delivery-minus-lead arithmetic that can cross it.
_DAY_MINUTES = 24 * 60

#: RFC 5545 weekday tokens mapped onto the numbering every backend here uses:
#: Sunday is 0, Monday is 1. It matches launchd's `Weekday` key directly.
WEEKDAY_NUMBER = {"SU": 0, "MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6}

#: The only recurrence parts translated into a backend's own idiom. Anything
#: else is refused by name instead of approximated: a rule that silently
#: understands only FREQ=DAILY turns every weekly entry into a single fire, and
#: the calendar looks plausible afterwards.
TRANSLATABLE_RRULE_PARTS = frozenset({"FREQ", "BYDAY", "INTERVAL"})
TRANSLATABLE_FREQUENCIES = frozenset({"DAILY", "WEEKLY"})


# ---------------------------------------------------------------------------
# The rendered result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderedFile:
    """One file that belongs on the machine, byte for byte."""

    path: str
    mode: int
    content: str


@dataclass(frozen=True)
class Artifact:
    """Everything one declaration becomes on one machine.

    `files` is a tuple because a wrapped workload has a unit AND a guard
    script, and both are part of its identity: an edited guard is drift just
    like an edited unit. The unit comes first, so a caller that wants "the
    unit" can take the first entry.
    """

    runtime: str
    unit_ref: str
    files: tuple
    digest: str
    guarantees_native: frozenset
    guarantees_wrapped: frozenset
    notes: str
    #: What THIS unit's state is filed under: the stamp, the trace, the guard,
    #: the captured output. `<id>` for a single appointment, `<id>.<name>`
    #: where a run has several. Carried on the artifact rather than recomputed
    #: from the label by whoever needs it: a second derivation of a name is the
    #: exact defect this whole round came out of. Defaulted so every backend
    #: that has not been touched keeps the declaration id, as before.
    state_key: str = ""


@dataclass(frozen=True)
class RenderContext:
    """The host facts a render must not guess.

    `uid` especially: a launchd target is `gui/<uid>` and that number is read
    off the box, never written into the code. A wrong domain is a plan pointing
    at a session that is not the one running the job.
    """

    uid: str
    home: str
    stamp_dir: str
    dispatcher_registry: Any
    host_timezone: str


def digest_of(files: Sequence[RenderedFile]) -> str:
    """sha256 over the sorted (path, mode, content) of the WHOLE file set.

    Sorted, so the tuple order never changes the identity. Over every file, so
    an edited guard script is drift and not an invisible difference.
    """
    accumulator = hashlib.sha256()
    for item in sorted(files, key=lambda f: str(f.path)):
        content = item.content
        if not isinstance(content, bytes):
            content = content.encode("utf-8")
        accumulator.update(str(item.path).encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(format(item.mode, "04o").encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(content)
        accumulator.update(b"\0")
    return "sha256:" + accumulator.hexdigest()


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

class Backend(Protocol):
    """What the registry holds. Four data attributes and the step seams.

    Three read seams, and they answer different questions on purpose:

    * ``inspect_steps`` / ``parse_inspection``: everything about ONE unit,
      including the ownership marker read back out of it. This is the seam
      ``provision.observe`` uses, which is why no service-manager output format
      is parsed anywhere outside this package.
    * ``discover_steps`` / ``parse_discovery``: enumerate what a machine
      carries, without asking about any declaration. ``reconcile`` uses it.
    * ``disabled_list_steps`` / ``parse_disabled``: the PERSISTENT off-list,
      which is a separate read: ``launchctl print`` does not carry it, and a
      unit somebody switched off deliberately must never be switched back on by
      a provision run. A backend that cannot answer returns no steps, and the
      answer is then ``None`` (unknown), never ``False`` (not disabled).
    """

    #: Platform slugs this backend exists on.
    platforms: frozenset
    #: `placement.kind` values it can carry.
    kinds: frozenset
    #: What it guarantees NATIVELY, without a guard script.
    guarantees: frozenset

    name: str

    def render(self, w, h, ctx) -> Artifact: ...

    def install_steps(self, a, h) -> tuple: ...

    def replace_steps(self, a, h) -> tuple: ...

    def disable_steps(self, a, h, reason: str) -> tuple: ...

    def uninstall_steps(self, a, h) -> tuple: ...

    def default_probe(self, a, h) -> Step: ...

    def inspect_steps(self, a, h) -> tuple: ...

    def parse_inspection(self, outs, unit_ref: str): ...

    def disabled_list_steps(self, a, h) -> tuple: ...

    def parse_disabled(self, outs, unit_ref: str): ...

    def discover_steps(self, h) -> tuple: ...

    def parse_discovery(self, outs) -> list: ...


def text_of(out) -> str:
    """The stdout of a recorded call, whatever shape the caller handed over."""
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    return getattr(out, "stdout", "") or ""


def label_of(unit_ref: str) -> str:
    """The bare label of a unit reference, without its domain."""
    return str(unit_ref or "").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Shared refusals
# ---------------------------------------------------------------------------

def ensure_platform(backend, host) -> None:
    """Refuse a runtime the platform does not carry, naming both."""
    platform = getattr(host, "platform", None)
    if platform not in backend.platforms:
        raise errors.UnsupportedRuntime(
            f"runtime {backend.name} is not available on platform {platform} "
            f"(host {getattr(host, 'slug', '?')}); this runtime exists on: "
            + ", ".join(sorted(backend.platforms))
        )


def ensure_kind(backend, w, remedy: str = "") -> None:
    """Refuse a kind the backend cannot carry, naming both."""
    kind = w.placement.kind
    if kind not in backend.kinds:
        message = (
            f"{backend.name} cannot carry kind {kind} for workload {w.id}; "
            f"it carries: " + ", ".join(sorted(backend.kinds))
        )
        if remedy:
            message = f"{message}. {remedy}"
        raise errors.UnsupportedKind(message)


def ensure_local_timezone(w, ctx, backend) -> None:
    """Refuse a declared zone the host does not run in.

    A calendar entry on these backends fires in the machine's own zone. When
    the declaration names a different one, the two agree today and drift by an
    hour at the next daylight-saving change, silently. An error now is cheaper
    than a report that arrives an hour late twice a year.
    """
    declared = getattr(w.schedule, "timezone", None) if w.schedule else None
    if not declared:
        return
    if declared != ctx.host_timezone:
        raise errors.UnsupportedTimezone(
            f"workload {w.id} is declared in {declared} but the host runs in "
            f"{ctx.host_timezone}, and {backend.name} places an appointment in "
            f"the machine's own zone only"
        )


# ---------------------------------------------------------------------------
# Shared schedule arithmetic
# ---------------------------------------------------------------------------

def only_appointment(w):
    """THE appointment, where a declaration has exactly one.

    Refuses instead of answering where there are several. A caller that wanted
    one unit and got the first of two would carry a guess that reads as a fact:
    the second appointment would never be rendered, never be provisioned and
    never be missed, because nothing would know it was owed.
    """
    appointments = appointments_of(w)
    if len(appointments) > 1:
        names = ", ".join(a.name or "?" for a in appointments)
        raise errors.UnsupportedRecurrence(
            f"workload {w.id} declares {len(appointments)} appointments "
            f"({names}); this asks for one appointment and answering with the "
            f"first would silently drop the rest. Use the plural entry point."
        )
    return appointments[0] if appointments else None


def appointments_of(w) -> tuple:
    """Every appointment of `w`, whatever built its schedule.

    The loader normalises the shorthand into this list, but a `Schedule`
    constructed in code (a test fake, a `dataclasses.replace`) carries only the
    raw fields, and a reader that trusted the list alone saw NO appointment
    where there plainly is one. So the fallback lives here, in the one function
    everything downstream asks, rather than in each of them.
    """
    declared = tuple(getattr(w.schedule, "appointments", ()) or ()) if w.schedule else ()
    if declared:
        return declared
    schedule = w.schedule
    if schedule and (getattr(schedule, "rrule", None)
                     or getattr(schedule, "delivery_at", None)):
        from engine.model import Appointment

        return (Appointment(name="",
                            at=getattr(schedule, "delivery_at", None),
                            rrule=getattr(schedule, "rrule", None),
                            duration_estimate_min=getattr(
                                schedule, "duration_estimate_min", None)),)
    return ()


def starts_of(w) -> tuple:
    """One (appointment, hour, minute, day_shift) per declared appointment."""
    out = []
    for appointment in appointments_of(w):
        hour, minute, shift = start_of(w, appointment)
        out.append((appointment, hour, minute, shift))
    return tuple(out)


def start_of(w, appointment=None) -> tuple:
    """(hour, minute, day_shift): the START, derived from the delivery time.

    `delivery_at` is when the RESULT is due, so the unit fires
    `duration_estimate_min` earlier. 00:10 minus twenty minutes is 23:50 on the
    PREVIOUS day, which is why the shift is returned rather than dropped: a
    backend that only subtracts the minutes moves a Monday job onto Monday
    night.
    """
    if appointment is None:
        appointment = only_appointment(w)
    delivery = getattr(appointment, "at", None) if appointment is not None else None
    if not delivery:
        delivery = getattr(w.schedule, "delivery_at", None) if w.schedule else None
    if not delivery:
        raise errors.UnsupportedRecurrence(
            f"workload {w.id} recurs but names no delivery_at, so there is no "
            f"time to place it at; inventing one would be a guess"
        )
    hours, _, minutes = delivery.partition(":")
    total = int(hours) * 60 + int(minutes)
    estimate = getattr(appointment, "duration_estimate_min", None) if appointment is not None else None
    if estimate is None:
        estimate = getattr(w.schedule, "duration_estimate_min", 0)
    total -= int(estimate or 0)
    shift = 0
    while total < 0:
        total += _DAY_MINUTES
        shift -= 1
    while total >= _DAY_MINUTES:
        total -= _DAY_MINUTES
        shift += 1
    return total // 60, total % 60, shift


def previous_due(w, appointment, now, zone):
    """The most recent moment `appointment` was due to FIRE, at or before `now`.

    This is what missing detection needs and what a cadence cannot give. An
    interval states its legitimate gap outright; a recurring run cannot, because
    between 06:30 and 12:30 that gap is six hours and between Saturday midday
    and Monday morning it is forty two.

    It needs no recurrence engine. The hour and minute come from `start_of` and
    the weekday set from `weekdays_of`, which are the same two functions the
    unit file is rendered from, so the check and the machine cannot disagree
    about when a job fires. Anything outside the translated subset raises out of
    those functions exactly as it does at render time: an approximation here
    would be a number nobody declared, presented as a measurement.

    `zone` is the DECLARED zone, and it is not optional. An appointment is wall
    clock and a trace is UTC; comparing them without a zone is wrong by the
    offset all year and by an hour more for half of it.

    Returns None only when nothing in the last eight days matched, which happens
    for a weekday set that is somehow empty. Never a guess.
    """
    from datetime import datetime, time, timedelta, timezone

    hour, minute, shift = start_of(w, appointment)
    days = weekdays_of(w, shift, appointment)
    local = now.astimezone(zone)
    best = None
    # Eight days back, so a run that fires on one weekday is still found from
    # any day of the following week.
    for back in range(0, 8):
        day = (local - timedelta(days=back)).date()
        # launchd numbering: Sunday 0 through Saturday 6. Python's own is
        # Monday 0 through Sunday 6, so the two are one apart, modulo seven.
        # `weekdays_of` already answers in launchd's numbering, and converting
        # HERE rather than there keeps one direction of translation instead of
        # two that can disagree.
        launchd_weekday = (day.weekday() + 1) % 7
        if days and launchd_weekday not in days:
            continue
        # A wall clock time in a real zone: this is where a DST transition is
        # handled rather than ignored. On the two ambiguous days a year the
        # fold rule picks one of the two, which is what launchd does too.
        candidate = datetime.combine(day, time(hour, minute), tzinfo=zone)
        if candidate <= local and (best is None or candidate > best):
            best = candidate
    return best.astimezone(timezone.utc) if best is not None else None


def weekdays_of(w, shift: int = 0, appointment=None) -> tuple:
    """The weekdays an rrule fires on, or () when it fires every day.

    Raises UnsupportedRecurrence, by name, for everything outside the
    translated subset. Never an approximation: the general evaluation of a
    recurrence rule belongs to the dispatcher, not to a provisioner.
    """
    if appointment is None:
        appointment = only_appointment(w)
    rule = (getattr(appointment, "rrule", None) if appointment is not None else None) or ""
    if not rule:
        rule = (getattr(w.schedule, "rrule", None) if w.schedule else None) or ""
    parts = {}
    for chunk in rule.split(";"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip().upper()] = value.strip().upper()

    untranslatable = sorted(set(parts) - TRANSLATABLE_RRULE_PARTS)
    frequency = parts.get("FREQ", "")
    interval = parts.get("INTERVAL", "1")
    if untranslatable or frequency not in TRANSLATABLE_FREQUENCIES or interval != "1":
        raise errors.UnsupportedRecurrence(
            f"workload {w.id} declares {rule!r}, which this backend cannot "
            f"express; translated are FREQ=DAILY and FREQ=WEEKLY with BYDAY, "
            f"both at INTERVAL=1. Evaluating the rest belongs to the "
            f"dispatcher, and approximating it here would fire on the wrong days"
        )

    tokens = [t for t in parts.get("BYDAY", "").split(",") if t]
    if frequency == "WEEKLY" and not tokens:
        raise errors.UnsupportedRecurrence(
            f"workload {w.id} declares {rule!r} without BYDAY, so which day it "
            f"means is a guess"
        )
    unknown = sorted(t for t in tokens if t not in WEEKDAY_NUMBER)
    if unknown:
        raise errors.UnsupportedRecurrence(
            f"workload {w.id} declares {rule!r}; the weekday token(s) "
            + ", ".join(unknown)
            + " are not plain RFC 5545 weekdays"
        )
    return tuple(sorted((WEEKDAY_NUMBER[t] + shift) % 7 for t in tokens))


# ---------------------------------------------------------------------------
# Small shared readers
# ---------------------------------------------------------------------------

def command_of(w) -> tuple:
    """The declared argv, verbatim.

    `placement.interpreter` is a client path a permission grant hangs off, so
    nothing here resolves, normalises or 'stabilises' any element of it: a
    resolved path is a DIFFERENT client with no grant.
    """
    execution = getattr(w, "execution", None)
    return tuple(getattr(execution, "command", ()) or ()) if execution else ()


def timeout_of(w):
    execution = getattr(w, "execution", None)
    return getattr(execution, "timeout_sec", None) if execution else None


def working_dir_of(w):
    execution = getattr(w, "execution", None)
    return getattr(execution, "working_dir", None) if execution else None


def env_of(w) -> Mapping:
    """The declared environment, with every NAME held to being a name.

    The check sits here rather than in each backend because this is the one seam
    all of them read it through, and one of them EXECUTES it: the guard script
    writes `NAME=value` and `export NAME` as shell, so a name carrying `;` runs
    a command, twice. systemd takes the name bare on the left of `Environment=`
    and launchd would carry it into the plist unchanged.
    """
    execution = getattr(w, "execution", None)
    declared = dict(getattr(execution, "env", None) or {}) if execution else {}
    for name in declared:
        ensure_env_name(name, workload_id=getattr(w, "id", ""))
    return declared


def evidence_of(w):
    response = getattr(w, "response", None)
    return getattr(response, "evidence", None) if response else None


def on_timeout_of(w) -> str:
    execution = getattr(w, "execution", None)
    return (getattr(execution, "on_timeout", None) if execution else None) or "report"


def marker_env(w, digest: str) -> dict:
    """The ownership marker in its plainest form: two environment entries."""
    return {MARKER_ENV_ID: w.id, MARKER_ENV_DIGEST: digest}


def shell_command(argv: Sequence[str]) -> str:
    """argv as one safely quoted /bin/sh command."""
    return " ".join(shlex.quote(str(a)) for a in argv)


#: How much of a file travels in ONE command line, by default.
#:
#: The size gate in `publish` counts a whole file against the SHELL's limit.
#: This is the smaller limit underneath it, and it belongs to the CONNECTION: a
#: multiplexed ssh session carries one request in one packet and refuses past
#: about 256 KiB with `mux_client_request_session: write packet: Broken pipe`,
#: an error naming neither the file nor its size nor the reason. Measured on
#: 2026-08-27 with a 274 KiB page that the gate had passed and the machine
#: would not take.
#:
#: Ninety-six leaves room for the quoting, the here-document delimiter and the
#: ssh command wrapper around each chunk.
CHUNK_BYTES = 96 * 1024


def _chunks(body: str, chunk_bytes: int) -> list:
    """`body` split on LINE boundaries, each part at most `chunk_bytes`.

    Only on line boundaries, and that is not tidiness: every part travels as a
    here-document, and a here-document ends every line it carries. A split
    inside a line would put a newline into the file that was never in it, and
    the read-back would then fail on a file that had been sent correctly.

    A single line longer than the limit becomes its own part rather than being
    cut. It may still be too big for the transport, and the size gate above is
    what says so in words.
    """
    parts, current, size = [], [], 0
    for line in body.splitlines(keepends=True):
        cost = len(line.encode("utf-8"))
        if current and size + cost > chunk_bytes:
            parts.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        parts.append("".join(current))
    return parts or [""]


def write_file_steps(item: RenderedFile, *, elevated: bool = False,
                     chunk_bytes: int = CHUNK_BYTES) -> tuple:
    """The same write as `write_file_step`, in as many parts as it needs.

    The first part truncates and the rest append, so the file on the machine is
    the concatenation of them in order. Nothing here proves that happened: the
    caller's read-back does, which is why chunking is safe to do at all. A half
    delivered file fails the comparison exactly like a corrupted one.
    """
    directory = str(item.path).rsplit("/", 1)[0] or "/"
    body = item.content if isinstance(item.content, str) else item.content.decode("utf-8")
    body = body if body.endswith("\n") else body + "\n"
    parts = _chunks(body, max(1, int(chunk_bytes)))
    steps = []
    for index, part in enumerate(parts):
        delimiter = "BRIDGE_WORKLOAD_EOF"
        while any(line.strip() == delimiter for line in part.splitlines()):
            delimiter += "_"
        lead = (f"mkdir -p {shlex.quote(directory)} && " if index == 0 else "")
        redirect = ">" if index == 0 else ">>"
        tail = ("\n" + f"chmod {format(item.mode, '04o')} "
                + shlex.quote(str(item.path))) if index == len(parts) - 1 else ""
        script = (
            f"{lead}cat {redirect} {shlex.quote(str(item.path))} <<'{delimiter}'\n"
            f"{part}{delimiter}{tail}"
        )
        steps.append(Step(
            argv=("/bin/sh", "-c", script),
            purpose=(f"write {item.path}" if len(parts) == 1 else
                     f"write {item.path}, part {index + 1} of {len(parts)}"),
            requires_elevation=elevated,
        ))
    return tuple(steps)


def write_file_step(item: RenderedFile, *, elevated: bool = False) -> Step:
    """A step that puts one rendered file on the machine, content and all.

    The content travels inside the argv as a quoted here-document, so the same
    step works over ssh (where stdin belongs to the connection and must stay
    closed) and in a printed plan a human can read before running it.
    """
    directory = str(item.path).rsplit("/", 1)[0] or "/"
    delimiter = "BRIDGE_WORKLOAD_EOF"
    body = item.content if isinstance(item.content, str) else item.content.decode("utf-8")
    while any(line.strip() == delimiter for line in body.splitlines()):
        delimiter += "_"
    heredoc = body if body.endswith("\n") else body + "\n"
    script = (
        f"mkdir -p {shlex.quote(directory)} && "
        f"cat > {shlex.quote(str(item.path))} <<'{delimiter}'\n"
        f"{heredoc}{delimiter}\n"
        f"chmod {format(item.mode, '04o')} {shlex.quote(str(item.path))}"
    )
    return Step(
        argv=("/bin/sh", "-c", script),
        purpose=f"write {item.path}",
        requires_elevation=elevated,
    )
