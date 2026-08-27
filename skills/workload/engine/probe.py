"""Probe resolution and evaluation, the read-only half of reconcile.

Three jobs:

* resolve WHICH probe answers for a workload: the declaration first, then a
  reference into the check registry, then the backend default;
* refuse what cannot be evaluated: an unresolved ``<placeholder>`` in the
  command, or prose instead of a pattern in ``expect``. Both yield *unknown*
  with the reason. Running the first would resolve a literal hostname;
  evaluating the second is a coin flip dressed as a check;
* evaluate an answer with the same vocabulary the check registry already uses
  (``workflow/checks/*.yaml``), so an operator learns one language, not two.

Every outbound call carries a deadline (rule 1) and goes out through
``engine.exec``, which starts it in its own session and kills the whole process
group when the deadline expires (rule 2). An expired probe is ``unknown``:
never a pass, never a fail, and never silence.

Nothing in this module changes anything on a machine.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from engine import model
from engine.errors import CheckRefAmbiguous, StepFailed, StepTimeout

#: Where the check registry lives, relative to the repo root. A structural
#: constant of the layout, overridable through the config when an instance
#: keeps its checks elsewhere.
DEFAULT_CHECKS_DIR = "workflow/checks"

#: An expect longer than this many words reads as a sentence, not as a pattern.
#: One real declaration says "no match, the label must stay absent", which no
#: matcher can honour; it has to come out as unknown, not as a guessed verdict.
MAX_EXPECT_WORDS = 4

_PROSE_MARKS = (",", ";", ": ", ". ")
_OPERATORS = (">=", "<=", "==", "!=", ">", "<")
_PLACEHOLDER = re.compile(r"<[A-Za-z0-9_.\-]+>")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


class Verdict(str, Enum):
    """What a probe answered. ``unknown`` is a first-class answer, not a gap."""

    pass_ = "pass"
    fail = "fail"
    unknown = "unknown"


@dataclass(frozen=True)
class ProbeSpec:
    """One resolved probe: what to run, what a healthy answer looks like."""

    command: str
    expect: str | None = None
    source: str = "unresolved"
    hint: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Outbound calls
# ---------------------------------------------------------------------------

def call_runner(runner, host, argv: Sequence[str], *, timeout_sec: int,
                connect_timeout_sec: int | None = None):
    """Run one read-only argv against ``host`` under a deadline.

    ``runner`` is injected by the caller (and by every test, which is how the
    suite stays away from real machines). Without one the call goes through
    ``engine.exec``: a new session, a process-group kill on expiry, and never a
    shell string. The rc is returned as data, so this path deliberately does
    not raise on a non-zero rc: a probe's rc IS its answer.
    """
    argv = tuple(str(a) for a in argv)
    if runner is not None:
        return runner(argv, timeout_sec=timeout_sec)

    from engine import exec as execution  # local: exec belongs to provision

    if getattr(host, "is_local", False):
        return execution.run_argv(argv, timeout_sec=timeout_sec)
    if connect_timeout_sec is None:
        # Strictly smaller than the deadline, otherwise the outer deadline is
        # the only one that ever fires and a dead host costs the full timeout.
        connect_timeout_sec = max(1, min(timeout_sec - 1, timeout_sec // 2 or 1))
    wrapped = execution.ssh_argv(host, argv, connect_timeout_sec=connect_timeout_sec)
    return execution.run_argv(tuple(wrapped), timeout_sec=timeout_sec)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_probe(w, h, artifact, root: Path, cfg) -> ProbeSpec:
    """Precedence: declared probe > check_ref > backend default.

    ``artifact`` may be None; the backend default needs it to know which unit
    to ask about, so without one the spec comes back unresolved instead of
    inventing a target.
    """
    rec = getattr(w, "reconcile", None)
    hint = _text(getattr(rec, "hint", None))

    command = _text(getattr(rec, "probe", None))
    if command:
        return ProbeSpec(command=command, expect=_text(getattr(rec, "expect", None)) or None,
                         source="declaration", hint=hint)

    ref = _text(getattr(rec, "check_ref", None))
    if ref:
        return _from_check_ref(ref, root, cfg, hint=hint)

    if artifact is not None:
        backend = _backend_for(w)
        if backend is not None:
            step = backend.default_probe(artifact, h)
            return ProbeSpec(command=shlex.join(tuple(str(a) for a in step.argv)),
                             expect=_alive_expect(backend, w),
                             source="backend-default", hint=hint)

    return ProbeSpec(command="", expect=None, source="unresolved", hint=hint,
                     reason="no probe declared, no check referenced and no artifact "
                            "to derive the backend default from")


def _alive_expect(backend, w):
    """What the backend default probe has to SEE for a run that never ends.

    `expect=None` means the return code decides, which is the right question for
    a run that ends and the wrong one for a run that does not. A service manager
    answers 0 for a unit it merely holds: `launchctl print` returns 0 for a
    loaded corpse, measured on a live machine on 2026-08-26. So a daemon was
    reported as verified while it was dead, one level below the docstring of
    `provision.ask_live_source`, which warns about exactly this.

    The expression is the BACKEND's, because only it knows the shape of its own
    answer, and it is asked for only where the kind makes the return code
    meaningless. A backend that does not name one leaves the old behaviour
    alone rather than inventing a pattern for an output it never produces.
    """
    kind = _text(getattr(getattr(w, "placement", None), "kind", None))
    if kind not in model.CONTINUOUS_KINDS:
        return None
    return _text(getattr(backend, "alive_expect", None)) or None


def _backend_for(w):
    # Imported here, not at module import: a backend's parse_discovery builds
    # reconcile.LiveUnit, so a top-level import would close the cycle.
    from engine import backends

    return backends.get_backend(_name(w.placement.runtime))


def _from_check_ref(ref: str, root: Path, cfg, *, hint: str = "") -> ProbeSpec:
    """Resolve ``<group>/<id>`` or a bare ``<id>`` against the check registry.

    A bare id that exists in more than one group raises instead of picking one:
    the two disk-free checks in the fixture registry measure different volumes
    on different hosts, and picking either would be a coin flip.
    """
    checks_dir = root / _text(getattr(cfg, "checks_dir", None) or DEFAULT_CHECKS_DIR)
    group, _, check_id = ref.rpartition("/")

    hits: list[tuple[str, Mapping[str, Any]]] = []
    for path in sorted(checks_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = _text(raw.get("group")) or path.stem
        if group and name != group:
            continue
        for check in raw.get("checks") or []:
            if _text(check.get("id")) == check_id:
                hits.append((name, check))

    if len(hits) > 1:
        groups = ", ".join(sorted(name for name, _ in hits))
        raise CheckRefAmbiguous(
            f"check ref {ref!r} names an id that exists in several groups: {groups}. "
            f"Qualify it as <group>/{check_id}."
        )
    if not hits:
        return ProbeSpec(command="", expect=None, source=f"check:{ref}", hint=hint,
                         reason=f"check ref {ref!r} resolves to nothing under {checks_dir}")

    name, check = hits[0]
    return ProbeSpec(command=_text(check.get("probe")),
                     expect=_text(check.get("expect")) or None,
                     source=f"check:{name}/{check_id}",
                     hint=hint or _text(check.get("hint")))


# ---------------------------------------------------------------------------
# What can and cannot be evaluated
# ---------------------------------------------------------------------------

def is_evaluatable(spec: ProbeSpec) -> tuple[bool, str]:
    """Answer whether this probe may be run and its answer judged.

    Returns (ok, reason). A False here becomes ``unknown`` further up, with the
    reason carried into the finding. Never a pass, never a fail.
    """
    command = (spec.command or "").strip()
    if not command:
        return False, spec.reason or f"no probe resolved ({spec.source})"

    placeholder = _PLACEHOLDER.search(command)
    if placeholder:
        return False, (f"the probe still carries the placeholder {placeholder.group(0)}; "
                       f"running it would resolve a literal hostname")

    expect = (spec.expect or "").strip()
    if not expect:
        return True, ""
    if expect.startswith(("re:", "not:")) or expect.startswith(_OPERATORS):
        return True, ""
    if any(mark in expect for mark in _PROSE_MARKS) or len(expect.split()) > MAX_EXPECT_WORDS:
        return False, (f"expect reads as prose rather than as a pattern: {expect!r}. "
                       f"Use a substring, re:, not: or a comparison operator.")
    return True, ""


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(spec: ProbeSpec, done) -> Verdict:
    """Judge one answer. Same vocabulary as the check registry.

    * no expect              -> the exit code decides
    * plain text             -> substring of stdout, whitespace normalised
    * ``re:``                -> regular expression search
    * ``not:``               -> the substring must be ABSENT
    * ``>= <= > < == !=``    -> numeric compare; a non-numeric answer is
                                unknown, never a fail
    """
    if done is None:
        return Verdict.unknown

    stdout = getattr(done, "stdout", "") or ""
    expect = (spec.expect or "").strip()
    if not expect:
        return Verdict.pass_ if getattr(done, "rc", 1) == 0 else Verdict.fail

    if expect.startswith("re:"):
        pattern = expect[3:].strip()
        try:
            return Verdict.pass_ if re.search(pattern, stdout) else Verdict.fail
        except re.error:
            return Verdict.unknown

    if expect.startswith("not:"):
        needle = _norm(expect[4:])
        if not needle:
            return Verdict.unknown
        return Verdict.fail if needle in _norm(stdout) else Verdict.pass_

    split = _split_operator(expect)
    if split is not None:
        op, operand = split
        value = "".join(stdout.split())
        if not _NUMBER.match(value) or not _NUMBER.match(operand):
            return Verdict.unknown
        return Verdict.pass_ if _compare(float(value), op, float(operand)) else Verdict.fail

    return Verdict.pass_ if _norm(expect) in _norm(stdout) else Verdict.fail


def run_probe(spec: ProbeSpec, h, *, timeout_sec: int, runner=None,
              connect_timeout_sec: int | None = None):
    """Run one probe and judge it. Returns (Completed | None, Verdict).

    A user authored probe is a shell string, so it runs as
    ``('/bin/sh', '-c', cmd)``, the single deliberate shell in this skill:
    bounded, in its own session, killed as a group. What cannot be evaluated is
    never executed.
    """
    ok, _reason = is_evaluatable(spec)
    if not ok:
        return None, Verdict.unknown

    argv = ("/bin/sh", "-c", spec.command)
    try:
        done = call_runner(runner, h, argv, timeout_sec=timeout_sec,
                           connect_timeout_sec=connect_timeout_sec)
    except StepTimeout:
        return None, Verdict.unknown
    except StepFailed as exc:
        done = getattr(exc, "completed", None)
        if done is None:
            return None, Verdict.unknown
    return done, evaluate(spec, done)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _name(value) -> str:
    return str(getattr(value, "value", value))


def _split_operator(expect: str):
    for op in _OPERATORS:
        if expect.startswith(op):
            return op, expect[len(op):].strip()
    return None


def _compare(value: float, op: str, operand: float) -> bool:
    return {
        ">=": value >= operand,
        "<=": value <= operand,
        ">": value > operand,
        "<": value < operand,
        "==": value == operand,
        "!=": value != operand,
    }[op]
