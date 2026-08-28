"""The host inventory side of reconcile: pairing, delta, and a printed patch.

Two responsibilities that belong together because both are pairing problems:

* deciding whether a live unit belongs to a declaration (``find_unit``), and
* deciding whether an inventory entry under ``infra/remotes/<host>.yaml``
  belongs to either of them (``match`` / ``match_one``).

A host inventory carries dozens of entries, so a wrong pairing invents drift
that is not there. Every rule here therefore refuses before it guesses: with
nothing to match on, or with several equally good candidates, the answer is an
``AmbiguousInventoryMatch``, not a pick.

This module never writes. ``proposed_patch`` returns a snippet for a human to
paste. This skill owns ``workflow/workloads/``, not the host inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from engine import model
from engine.errors import AmbiguousInventoryMatch
from engine.report import Finding

#: Pairing order. The label is the strongest signal, the command path the
#: weakest, because two services can share a command and differ in all else.
MATCH_FIELDS = ("label", "slug", "command")


@dataclass(frozen=True)
class ServiceEntry:
    """One ``services[]`` entry, tolerant of keys this skill does not know.

    Real inventories carry hand-written keys in the operator's own language.
    An unknown key is kept in ``raw`` and never crashes the reader.
    """

    slug: str
    label: str | None = None
    command: str | None = None
    type: str | None = None
    status: str | None = None
    #: The two halves of `intentionally_absent`, the field open-bridge#159 put
    #: into the host contract. Both empty means the entry says nothing about
    #: why it names something the machine does not have, which is a different
    #: answer from saying it is gone on purpose.
    absent_since: str = ""
    absent_reason: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def decided_absent(self) -> bool:
        """Whether a person wrote down that this is gone on purpose.

        A date OR a reason is enough. Requiring both would let a half filled
        block reopen a decision, and the missing half is a gap in the record
        rather than evidence that the decision was never taken.
        """
        return bool(self.absent_since or self.absent_reason)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_inventory(h) -> dict[str, ServiceEntry]:
    """The ``services[]`` block of one host, keyed by slug.

    The host object already carries the parsed inventory, so this never opens a
    file of its own and never reads a credential.
    """
    entries: dict[str, ServiceEntry] = {}
    for raw in getattr(h, "services", None) or []:
        if not isinstance(raw, Mapping):
            continue
        slug = _text(raw.get("slug")) or _text(raw.get("label"))
        if not slug:
            continue
        # A MAPPING or nothing. A hand written `intentionally_absent:
        # "parkiert"` is a broken block, not a decision, and reading it as one
        # would let a typo switch a report off. Same rule, and for the same
        # reason, as scripts/bridge-ops-evaluate.py: this field is the one that
        # suppresses both the report and the repair, so it is the last place to
        # be generous with what it accepts.
        #
        # Only the nested English field is read. The flat keys some inventories
        # carry in their operator's own language stay unread here: a
        # `scope: core` skill that learns one instance's vocabulary has stopped
        # being generic, and the nested block is the half the schema checks.
        gone = raw.get("intentionally_absent")
        gone = gone if isinstance(gone, Mapping) else {}
        entries[slug] = ServiceEntry(
            slug=slug,
            label=_text(raw.get("label")) or None,
            command=_text(raw.get("command")) or None,
            type=_text(raw.get("type")) or None,
            status=_text(raw.get("status")) or None,
            absent_since=_text(gone.get("since")),
            absent_reason=_text(gone.get("reason")),
            raw=dict(raw),
        )
    return entries


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def match(entry: ServiceEntry, other) -> bool:
    """Does ``other`` describe the same service as ``entry``?

    Compared on the first field both sides actually carry, in MATCH_FIELDS
    order. A field only one side knows says nothing and is skipped; when the
    two sides share no field at all the answer is False, and the caller that
    needs a decision uses ``match_one``, which refuses instead.
    """
    for name in MATCH_FIELDS:
        mine = _text(getattr(entry, name, None))
        theirs = _text(_get(other, name))
        if mine and theirs:
            return mine == theirs
    return False


def match_one(entries: Mapping[str, ServiceEntry], other) -> ServiceEntry | None:
    """The single entry ``other`` describes, or None. Never a guess.

    Raises AmbiguousInventoryMatch when there is nothing to match on, or when
    several entries match equally well.
    """
    usable = [name for name in MATCH_FIELDS if _text(_get(other, name))]
    if not usable:
        raise AmbiguousInventoryMatch(
            "nothing to match an inventory entry on: the candidate carries neither "
            f"{', '.join(MATCH_FIELDS)}. Any answer here would be a guess."
        )
    hits = [entry for entry in entries.values() if match(entry, other)]
    if len(hits) > 1:
        names = ", ".join(sorted(hit.slug for hit in hits))
        raise AmbiguousInventoryMatch(
            f"several inventory entries match on {', '.join(usable)}: {names}"
        )
    return hits[0] if hits else None


def find_units(workload, live_units: Iterable, stamp=None) -> tuple:
    """EVERY live unit that belongs to ``workload``, in the order seen.

    A declaration with several appointments is several units, all carrying the
    same ownership marker because they come from the same declaration. Taking
    the first and stopping left the rest claimed by nobody, and the report then
    said they run on the machine with no declaration behind them: a unit called
    foreign while its own declaration sits in the repository, which is the
    reading that teaches somebody to stop believing the report.

    Same three rules as ``find_unit``, applied to all of them, and each unit is
    returned once however many rules match it.
    """
    units = list(live_units or ())
    stamped_ref = _text(getattr(stamp, "unit_ref", None))
    names = unit_names_of(workload)   # once, not once per unit
    found, seen = [], set()
    for unit in units:
        if unit.unit_ref in seen:
            continue
        belongs = (
            (unit.marker_id and unit.marker_id == workload.id)
            or (stamped_ref and unit.unit_ref == stamped_ref)
            or label_of(unit.unit_ref) in names
        )
        if belongs:
            seen.add(unit.unit_ref)
            found.append(unit)
    return tuple(found)


def find_unit(workload, live_units: Iterable, stamp=None):
    """The live unit that belongs to ``workload``, or None.

    Three rules, strongest first:

    1. the ownership marker read back out of the live unit names this workload,
    2. the stamp on the host records exactly this unit reference,
    3. the label is one the backend would produce for this declaration, asked
       of the backend rather than guessed from the id, and covering every
       declared appointment.

    Rule 3 is what lets a hand-made unit be recognised as *unstamped* rather
    than disappear into the unmanaged pile, which is the difference between a
    refusal and an overwrite. It is also the rule that carries a declaration
    whose two ownership signals are BOTH gone, which is the case on a fresh
    machine, a restored one, or after `stamp_dir` was pointed somewhere else.
    """
    units = list(live_units or ())
    for unit in units:
        if unit.marker_id and unit.marker_id == workload.id:
            return unit
    stamped_ref = _text(getattr(stamp, "unit_ref", None))
    if stamped_ref:
        for unit in units:
            if unit.unit_ref == stamped_ref:
                return unit
    names = unit_names_of(workload)   # once, not once per unit
    for unit in units:
        if label_of(unit.unit_ref) in names:
            return unit
    return None


def label_of(unit_ref: str) -> str:
    """The bare label of a unit reference (``gui/<uid>/<label>`` -> label)."""
    return _text(unit_ref).rsplit("/", 1)[-1]


def unit_names_of(workload) -> frozenset:
    """Every name this declaration's backend would give it on a machine.

    ASKED of the backend, never rebuilt here. A second derivation of a name is
    how a migrated run was filed as foreign software on 2026-08-24, and the
    rule below is the one place where a wrong name is INVISIBLE rather than
    loud: it does not fail, it files a unit under the wrong owner.

    Empty for the runtimes that name nothing on the machine (cron, dispatcher,
    manual, external). Rule 3 then simply does not fire for them, which is
    correct: there is no name to recognise.
    """
    from engine import backends as backends_mod
    from engine.backends import base as base_mod

    try:
        backend = backends_mod.get_backend(workload.placement.runtime)
    except Exception:
        return frozenset()
    names = set()
    for appointment in (base_mod.appointments_of(workload) or (None,)):
        try:
            name = backend.unit_name(workload, appointment)
        except Exception:
            continue
        if name:
            names.add(name)
    return frozenset(names)


def label_matches(unit_ref: str, workload) -> bool:
    """Does this live unit carry a name THIS declaration would produce.

    EXACT, against the set above. Until 2026-08-25 this compared string ends
    (`== id`, `endswith .id`, `startswith id.`) and got both directions wrong
    at once:

      * TOO NARROW for appointments. `bridge.<id>.<appointment>` matches none
        of the three forms, so a declaration with several appointments lost its
        own units to the unmanaged pile the moment both ownership signals were
        gone, and reported ITSELF as never provisioned. Two loud failures that
        cancel into one quiet wrong picture.
      * TOO WIDE at the front. `endswith` is not anchored, so a unit under a
        completely foreign prefix was claimed. That is how a superseded unit
        and its successor were both reported in sync against one stamp.

    Loosening one without anchoring the other trades one wrong match for
    another, which is why both changed together.
    """
    label = label_of(unit_ref)
    return bool(label) and label in unit_names_of(workload)


# ---------------------------------------------------------------------------
# The delta
# ---------------------------------------------------------------------------

def inventory_delta(host_obs, inventory: Mapping[str, ServiceEntry],
                    workloads: Sequence) -> list[Finding]:
    """Inventory-anchored findings: what runs but is not listed, and the reverse.

    Only what is BOTH declared and live can be missing from the inventory. A
    declaration nobody provisioned yet does not belong in a host's service
    list.
    """
    findings: list[Finding] = []
    claimed: set[str] = set()
    units = list(getattr(host_obs, "live_units", ()) or ())
    host = _text(getattr(host_obs, "host", "")) or "the host"

    for w in workloads:
        unit = find_unit(w, units, getattr(host_obs, "stamps", {}).get(w.id))
        if unit is None:
            continue
        candidate = {"label": label_of(unit.unit_ref), "slug": w.id}
        try:
            entry = match_one(inventory, candidate)
        except AmbiguousInventoryMatch as exc:
            findings.append(Finding(
                workload_id=w.id, state=model.WorkloadState.unknown,
                severity=model.Severity.medium,
                detail=f"the inventory of {host} pairs ambiguously: {exc}",
                hint="give the entries distinct labels, then reconcile again",
                source="inventory"))
            continue
        if entry is None:
            findings.append(Finding(
                workload_id=w.id, state=model.WorkloadState.inventory_missing,
                severity=model.Severity.medium,
                detail=(_DETAIL_SHAPE.format(host=host, unit_ref=unit.unit_ref,
                                             runtime=unit.runtime)
                        + " but the inventory lists no services entry"),
                hint=f"add the entry to infra/remotes/{host}.yaml; reconcile --propose-inventory prints it",
                source="inventory"))
        else:
            claimed.add(entry.slug)

    unmatched: list[str] = []
    for slug, entry in inventory.items():
        if slug in claimed:
            continue
        if any(match(entry, {"slug": w.id, "label": None}) or _entry_names(entry, w)
               for w in workloads):
            continue
        if any(match(entry, {"label": label_of(unit.unit_ref)}) for unit in units):
            continue
        unmatched.append(slug)

    # Split BEFORE the guard below, because the two halves answer different
    # questions. `neither a declaration nor the machine knows it` is a claim
    # about the machine and has to be earned by a complete look. An entry that
    # says it is gone on purpose makes no claim about the machine at all: it
    # repeats what the file says, so nothing about the enumeration can hold it
    # back, and holding it back would lose the one row that needed no looking.
    decided = [slug for slug in unmatched
               if getattr(inventory.get(slug), "decided_absent", False)]
    for slug in decided:
        entry = inventory[slug]
        findings.append(Finding(
            workload_id=slug, state=model.WorkloadState.intentionally_absent,
            severity=model.Severity.info,
            detail=(f"the inventory of {host} lists {slug!r} and nothing runs it, "
                    f"{_absence_phrase(entry)}"),
            # Never `drop the entry`. The entry IS the record of the decision,
            # and the reason it carries is the answer to the question a reader
            # arrives with.
            hint=("nothing to do: this entry records a decision rather than a gap"
                  if entry.absent_since and entry.absent_reason else
                  "nothing to do, though the record is half written: "
                  + ("it names no date" if not entry.absent_since
                     else "it names no reason")),
            source="inventory"))
    if decided:
        taken = set(decided)
        unmatched = [slug for slug in unmatched if slug not in taken]

    # `neither a declaration nor the machine knows it` is a claim about the
    # MACHINE, and its hint says to delete the entry. A runtime that could not
    # be enumerated makes both unearned: nothing looked where those units live.
    # This is the same distinction the marker and `reachable` already carry --
    # not asked is not absent -- and it is stated once about the look rather
    # than repeated as a wrong verdict per entry.
    unlooked = sorted(getattr(host_obs, "failed_runtimes", None) or ())
    if unlooked and unmatched:
        findings.append(Finding(
            workload_id="", state=model.WorkloadState.unknown,
            severity=model.Severity.info,
            detail=(f"{len(unmatched)} inventory entr{'y' if len(unmatched) == 1 else 'ies'} "
                    f"of {host} could not be checked, because "
                    f"{', '.join(unlooked)} could not be enumerated: "
                    f"{', '.join(unmatched)}"),
            hint="fix the enumeration, then reconcile again; nothing here is a verdict yet",
            source="inventory"))
        return findings

    for slug in unmatched:
        findings.append(Finding(
            workload_id=slug, state=model.WorkloadState.inventory_stale,
            severity=model.Severity.info,
            detail=(f"the inventory of {host} lists {slug!r}, but neither a declaration "
                    f"nor the machine knows it"),
            hint=f"declare it as a workload or drop the entry from infra/remotes/{host}.yaml",
            source="inventory"))
    return findings


def _absence_phrase(entry: ServiceEntry) -> str:
    """How one decided absence reads, with whichever half was written down.

    Both halves are printed in full. A reason is somebody's sentence about why
    something is off, and it is the whole value of the record: truncating it
    here would leave a reader with a date and a question.
    """
    if entry.absent_since and entry.absent_reason:
        return f"which is on purpose since {entry.absent_since}: {entry.absent_reason}"
    if entry.absent_since:
        return (f"which is on purpose since {entry.absent_since}; "
                "the entry records no reason")
    return f"which is on purpose; the entry records no date: {entry.absent_reason}"


def proposed_patch(findings: Iterable[Finding]) -> str:
    """A printable ``services[]`` snippet. Printed for a human, never written."""
    rows = []
    for finding in findings:
        if finding.state is not model.WorkloadState.inventory_missing:
            continue
        unit_ref, runtime = _parse_detail(finding.detail)
        rows.append((finding.workload_id, label_of(unit_ref), runtime))

    lines = ["# Proposed additions to infra/remotes/<host>.yaml, printed and never written.",
             "# Review the wording, then paste them into the host's services block.",
             "services:"]
    if not rows:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for slug, label, runtime in rows:
        lines.append(f"  - slug: {slug}")
        if label:
            lines.append(f"    label: {label}")
        if runtime:
            lines.append(f"    type: {runtime}")
        lines.append(f"    purpose: \"see workflow/workloads/{slug}.yaml\"")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: inventory_delta writes the unit reference and the runtime into the detail in
#: this shape, and proposed_patch reads them back out of it. The two halves are
#: one unit: change the sentence and this pattern changes with it.
_DETAIL_SHAPE = "runs on {host} as {unit_ref} ({runtime})"


def _parse_detail(detail: str) -> tuple[str, str]:
    text = _text(detail)
    marker = " as "
    if marker not in text or "(" not in text:
        return "", ""
    tail = text.split(marker, 1)[1]
    unit_ref = tail.split(" (", 1)[0].strip()
    runtime = tail.split("(", 1)[1].split(")", 1)[0].strip()
    return unit_ref, runtime


def _entry_names(entry: ServiceEntry, workload) -> bool:
    return bool(entry.label) and label_matches(entry.label, workload)


def _get(other, name):
    if isinstance(other, Mapping):
        return other.get(name)
    return getattr(other, name, None)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
