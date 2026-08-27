"""One declaration in, the exact bytes that belong on the machine out.

Pure: no filesystem, no child process, no clock. The same declaration renders the
same bytes forever, which is what makes a second provision run a no-op instead
of a hope, and what makes a difference on disk mean drift instead of noise.

There is exactly one dispatch here, a dictionary lookup in the registry, and
zero comparisons against the name of a runtime or a platform. Every capability
a backend has is DATA it declares: which platforms carry it, which kinds it
takes, and what it promises without help. This module does the arithmetic
between what a declaration demands and what the backend answers for, and it
records what stays unanswered instead of pretending it is covered.
"""

from __future__ import annotations

import dataclasses

from engine.backends import base, get_backend, wrapper
from engine.model import Severity, WorkloadState, required_guarantees
from engine.report import Finding


def render_all(w, h, ctx) -> tuple:
    """Every unit `w` needs on `h`: one per declared appointment.

    A declaration with a single appointment (which is nearly all of them)
    answers with a one element tuple carrying exactly the artifact `render`
    would have produced, byte for byte. Several appointments answer with one
    unit each, because a launchd unit has exactly one command and exactly one
    environment: two times that answer different recipients cannot share one.

    This is the plural entry point every caller that WRITES should use.
    `render` stays for the single-unit question and refuses, rather than
    guessing, where a run has several.
    """
    appointments = base.appointments_of(w)
    if len(appointments) <= 1:
        return (render(w, h, ctx),)
    return tuple(render(w, h, ctx, appointment=a) for a in appointments)


def render(w, h, ctx, appointment=None) -> base.Artifact:
    """The bytes for `w` on `h`, plus what is and is not guaranteed about them."""
    backend = get_backend(w.placement.runtime)
    base.ensure_platform(backend, h)
    base.ensure_kind(backend, w, getattr(backend, "kind_remedy", ""))

    artifact = (backend.render(w, h, ctx, appointment=appointment)
                if appointment is not None else backend.render(w, h, ctx))

    covered = frozenset(artifact.guarantees_native) | frozenset(artifact.guarantees_wrapped)
    unmet = frozenset(required_guarantees(w)) - covered
    return dataclasses.replace(artifact, notes=_notes(artifact, unmet))


def preflight(w, h) -> list:
    """What would refuse or degrade, before anything is touched.

    Pure, so it can be shown to a person and then relied on: `provision` calls
    it before it observes anything, and the plan it prints is the same verdict
    a later run reaches.
    """
    backend = get_backend(w.placement.runtime)
    found = []

    if not getattr(backend, "provisionable", True):
        return [_finding(
            w, Severity.info,
            f"runtime {backend.name} is never provisioned from a declaration",
            "the declaration keeps the run visible; start and stop it the way "
            "it was started before",
        )]

    platform = getattr(h, "platform", None)
    if platform not in backend.platforms:
        found.append(_finding(
            w, Severity.high,
            f"runtime {backend.name} is not available on platform {platform} "
            f"(host {getattr(h, 'slug', '?')})",
            "declare a runtime this platform carries, or move the run to a "
            "host that carries this one",
        ))

    if w.placement.kind not in backend.kinds:
        found.append(_finding(
            w, Severity.high,
            f"runtime {backend.name} cannot carry kind {w.placement.kind}",
            getattr(backend, "kind_remedy", "") or "declare a kind it carries",
        ))

    if getattr(backend, "requires_elevation", False):
        found.append(_finding(
            w, Severity.medium,
            f"runtime {backend.name} needs elevation, which this skill never takes",
            "the steps are printed for a person to run, and the next run "
            "verifies the result",
        ))

    unmet = frozenset(required_guarantees(w)) - (
        frozenset(backend.guarantees) | wrapper.supplies(w, backend))
    if unmet:
        found.append(_finding(
            w, Severity.medium,
            f"runtime {backend.name} cannot answer for: {_names(unmet)}",
            "carry it with a runtime that can, or accept the degradation "
            "deliberately",
        ))
    return found


def _finding(w, severity, detail: str, hint: str) -> Finding:
    # The state enum is closed and describes what reconcile SAW. A preflight
    # verdict is about what would happen, so it takes the member that claims
    # the least rather than inventing a new one.
    return Finding(
        workload_id=w.id,
        state=WorkloadState.unknown,
        severity=severity,
        detail=detail,
        hint=hint,
        source="declaration",
    )


def _names(guarantees) -> str:
    return ", ".join(sorted(
        str(g.value if hasattr(g, "value") else g) for g in guarantees))


def _notes(artifact, unmet) -> str:
    parts = []
    if artifact.notes:
        parts.append(artifact.notes)
    if artifact.guarantees_native:
        parts.append("guaranteed by the runtime: " + _names(artifact.guarantees_native))
    if artifact.guarantees_wrapped:
        parts.append("supplied by the guard script: " + _names(artifact.guarantees_wrapped))
    if unmet:
        # Named, not swallowed: a guarantee nobody answers for is the thing an
        # operator has to know about before the run matters.
        parts.append("NOT guaranteed by anything here: " + _names(unmet))
    return "; ".join(parts)
