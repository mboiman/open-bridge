"""Resolve `response.recipients` against the mandants this Bridge declares.

A declaration names WHO is to be told by reference, never by address, because
it is a tracked file that travels with the scope router. The schema enforced
that shape and stopped there, and `model.py` said so about itself: "no check
that the slug exists". A typo in a slug therefore passed `validate`, passed
`provision`, and left a run that reports to nobody while the file still states
who was meant to hear about it.

THREE ANSWERS, not two, and the third is the load bearing one. A checkout may
hold no mandants at all: the OSS upstream ships `_schema.yaml` and
`_template.yaml` and not a single instance. A resolver with only `resolved` and
`unknown` would call every reference there wrong and refuse every declaration in
the repository. So "there is nothing here to check against" is its own verdict,
it is never a finding, and it says which directory it looked in.

This reads files, so it lives beside `_hollow_failure_promises` in the command
rather than inside `model.validate`, which is a pure invariant gate and has
never had a repository root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .model import Severity, WorkloadState
from .report import Finding

#: Where an instance keeps its recipient groups.
MANDANTS_DIR = "identity/mandants"

RESOLVED = "resolved"
MANDANT_UNKNOWN = "mandant_unknown"
PERSON_UNKNOWN = "person_unknown"
#: Nothing to check against, which is not the same as checked and wrong.
NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True)
class Resolution:
    """One recipient entry and what became of it.

    One per entry, always, and never a joined string: the first person whose
    display name holds a comma would otherwise silently become two people.
    """

    mandant: str
    person: str
    state: str
    detail: str


def _instances(folder: Path) -> dict:
    """The mandant files an instance declares. `_`-prefixed names are reserved."""
    if not folder.is_dir():
        return {}
    return {path.stem: path for path in sorted(folder.glob("*.yaml"))
            if not path.name.startswith("_")}


def _person_ids(path: Path):
    """The `persons[].id` set of one mandant, or None when it cannot be read.

    None is not an empty set. An unreadable file means the question was not
    answered, and saying "that person is not in there" from a file nobody could
    parse would be an invention.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    people = raw.get("persons")
    if not isinstance(people, list):
        return None
    return {str(entry.get("id")) for entry in people
            if isinstance(entry, dict) and entry.get("id")}


def resolve(workload, root) -> list:
    """Every declared recipient of `workload`, each with its own verdict."""
    entries = list(getattr(getattr(workload, "response", None), "recipients", None) or ())
    if not entries:
        return []
    folder = Path(root) / MANDANTS_DIR
    known = _instances(folder)
    out = []
    for entry in entries:
        mandant = str(getattr(entry, "mandant", "") or "")
        person = str(getattr(entry, "person", "") or "")
        if not known:
            out.append(Resolution(
                mandant, person, NOT_VERIFIABLE,
                f"{mandant}: no mandants are declared under {MANDANTS_DIR}/, so "
                f"this reference cannot be checked here"))
            continue
        if mandant not in known:
            out.append(Resolution(
                mandant, person, MANDANT_UNKNOWN,
                f"{mandant}: no such mandant under {MANDANTS_DIR}/ "
                f"(declared: {', '.join(sorted(known)) or 'none'})"))
            continue
        if not person:
            out.append(Resolution(mandant, person, RESOLVED,
                                  f"{mandant}: the whole group"))
            continue
        people = _person_ids(known[mandant])
        if people is None:
            out.append(Resolution(
                mandant, person, NOT_VERIFIABLE,
                f"{mandant}: {known[mandant].name} could not be read for its "
                f"persons, so {person} was not checked"))
        elif person in people:
            out.append(Resolution(mandant, person, RESOLVED,
                                  f"{mandant}/{person}"))
        else:
            out.append(Resolution(
                mandant, person, PERSON_UNKNOWN,
                f"{mandant}/{person}: {mandant} declares no person with that id "
                f"(declared: {', '.join(sorted(people)) or 'none'})"))
    return out


#: What a wrong reference costs: the run does its work and tells nobody, while
#: the declaration still names somebody. That is the failure this exists for.
_SEVERITY = {MANDANT_UNKNOWN: Severity.high, PERSON_UNKNOWN: Severity.high}

_HINT = {
    MANDANT_UNKNOWN: f"declare the mandant under {MANDANTS_DIR}/, or point the "
                     f"declaration at one that exists",
    PERSON_UNKNOWN: "add the person to that mandant's `persons:`, or name one "
                    "it already lists",
}


def findings_for(workloads, root) -> list:
    """The wrong references only. `not_verifiable` is never a finding."""
    out = []
    for workload in workloads:
        for one in resolve(workload, root):
            if one.state not in _SEVERITY:
                continue
            out.append(Finding(
                workload_id=workload.id,
                state=WorkloadState.unknown,
                severity=_SEVERITY[one.state],
                detail=f"response.recipients: {one.detail}",
                hint=_HINT[one.state],
                source="declaration",
            ))
    return out
