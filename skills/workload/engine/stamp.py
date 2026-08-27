"""The ownership record ON the machine.

A stamp answers "is this ours" without the repository being present, which is
what lets reconcile work from the box alone. It is the first of the two
independent ownership signals; the second is the marker inside the artifact
itself. Two blind procedures that can only agree would prove nothing, so a stamp
without a marker is drift, and so is a marker without a stamp.

The file carries no username and no secret: an id, a declaration path, two
digests, the unit reference and a timestamp.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from engine import exec as exec_mod
from engine import model

_TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class Stamp:
    """One provisioned workload, as recorded on the machine that carries it."""

    stamp_version: int
    workload_id: str
    host: str
    declaration: str
    declaration_digest: str
    artifact_digest: str
    runtime: str
    unit_ref: str
    files: tuple
    provisioned_at: str
    adopted: bool = False
    retired: dict | None = None
    #: What this record is FILED under: `<id>` for a single appointment,
    #: `<id>.<name>` where a run has several. Defaulted to empty so that every
    #: record written before this field existed still means what it meant, and
    #: `stamp_file` falls back to the workload id for those.
    #:
    #: Measured on a real machine on 2026-08-24: both units of a migrated run
    #: were provisioned and verified, and the machine carried ONE stamp. The
    #: second had written over the first, so a running unit was
    #: indistinguishable from one that had never been provisioned.
    state_key: str = ""
    #: The interpreter path as it stood when this was provisioned, or None on a
    #: stamp written before this field existed. It is here because it is the one
    #: provisioned value whose consequence OUTLIVES the file: a privacy grant is
    #: issued to a literal path and stays there when the declaration names a new
    #: one. Nothing else can notice that, because macOS lets no program read the
    #: grant database. None means the question cannot be answered from this
    #: stamp, which is a different answer from "unchanged" and is reported as
    #: such rather than passed over.
    interpreter: str | None = None


# ── serialisation ────────────────────────────────────────────────────────────

def to_json(stamp: Stamp) -> str:
    """Sorted keys, UTF-8, trailing newline. Deterministic, so it never drifts."""
    payload = dataclasses.asdict(stamp)
    payload["files"] = list(stamp.files)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"


def from_json(text: str) -> Stamp:
    raw = json.loads(text)
    raw["files"] = tuple(raw.get("files") or ())
    known = {f.name for f in dataclasses.fields(Stamp)}
    return Stamp(**{k: v for k, v in raw.items() if k in known})


# ── paths ────────────────────────────────────────────────────────────────────

def dir_expr(stamp_dir: str) -> str:
    """A directory the HOST expands, never this machine.

    A configured ``~`` or ``$HOME`` stays a shell expression, so the path is
    resolved where the file is written.
    """
    stamp_dir = str(stamp_dir or "")
    if stamp_dir.startswith("~/"):
        return '"$HOME"/' + stamp_dir[2:]
    if stamp_dir == "~":
        return '"$HOME"'
    if stamp_dir.startswith("$HOME"):
        return '"$HOME"' + stamp_dir[len("$HOME"):]
    return exec_mod.sh_quote(stamp_dir)


def _file_expr(stamp_dir: str, workload_id: str) -> str:
    return dir_expr(stamp_dir) + "/" + exec_mod.sh_quote(workload_id + model.STAMP_SUFFIX)


def key_of(stamp) -> str:
    """What a record is filed under. The unit, falling back to the declaration."""
    return (str(getattr(stamp, "state_key", "") or "").strip()
            or str(getattr(stamp, "workload_id", "") or ""))


def stamp_file(stamp_dir: str, stamp) -> str:
    """The path a record is written to, for a caller that wants to compare."""
    return f"{str(stamp_dir).rstrip('/')}/{key_of(stamp)}{model.STAMP_SUFFIX}"


# ── writing ──────────────────────────────────────────────────────────────────

def write_stamp(stamp: Stamp, host, ctx, *, timeout_sec, runner=None) -> None:
    """Write the record atomically: a temporary file, then a rename over it.

    A stamp written in place can be truncated by a crash, and a truncated stamp
    is worse than a missing one because it looks like an answer.
    """
    runner = runner or exec_mod.step_runner
    directory = dir_expr(ctx.stamp_dir)
    target = _file_expr(ctx.stamp_dir, key_of(stamp))
    temp = target + _TMP_SUFFIX
    script = (
        "set -e\n"
        f"mkdir -p {directory}\n"
        f"cat > {temp} <<'BRIDGE_WORKLOAD_STAMP'\n"
        f"{to_json(stamp)}"
        "BRIDGE_WORKLOAD_STAMP\n"
        f"mv {temp} {target}\n"
    )
    step = model.Step(
        argv=("/bin/sh", "-c", script),
        purpose=f"record ownership of {stamp.workload_id} on the machine",
    )
    runner(step, host, timeout_sec=timeout_sec)


def mark_retired(stamp: Stamp, host, ctx, *, at: str, reason: str,
                 superseded_by=None, timeout_sec, runner=None) -> Stamp:
    """Record the retirement in the stamp, so the box says why it stopped."""
    retired = {"at": at, "reason": reason}
    if superseded_by:
        retired["superseded_by"] = superseded_by
    updated = dataclasses.replace(stamp, retired=retired)
    write_stamp(updated, host, ctx, timeout_sec=timeout_sec, runner=runner)
    return updated


def remove_stamp(workload_id: str, host, ctx, *, timeout_sec, runner=None) -> None:
    runner = runner or exec_mod.step_runner
    step = model.Step(
        argv=("/bin/sh", "-c", f"rm -f {_file_expr(ctx.stamp_dir, workload_id)}"),
        purpose=f"drop the ownership record of {workload_id}",
    )
    runner(step, host, timeout_sec=timeout_sec)


# ── reading (what reconcile imports) ─────────────────────────────────────────

def read_stamp(host, stamp_dir, workload_id, *, timeout_sec, runner=None):
    """One record, or None. Read only.

    `workload_id` is the STATE KEY, not necessarily the declaration id: where a
    run has several appointments each unit files its own record under
    `<id>.<appointment>`. Callers pass `model.state_key(w, appointment)`.
    """
    runner = runner or exec_mod.step_runner
    step = model.Step(
        argv=("/bin/sh", "-c",
              f"cat {_file_expr(stamp_dir, workload_id)} 2>/dev/null || true"),
        purpose=f"read the ownership record of {workload_id}",
        expect_rc=(),
    )
    done = runner(step, host, timeout_sec=timeout_sec)
    return _first_stamp(done.stdout)


def read_stamps(host, cfg, *, timeout_sec, runner=None) -> dict:
    """Every record on the host, keyed by UNIT reference. Read only."""
    runner = runner or exec_mod.step_runner
    directory = dir_expr(cfg.stamp_dir)
    script = (
        f"for f in {directory}/*{model.STAMP_SUFFIX}; do "
        '[ -f "$f" ] || continue; cat "$f"; echo; done 2>/dev/null || true'
    )
    step = model.Step(
        argv=("/bin/sh", "-c", script),
        purpose="read every ownership record on the host",
        expect_rc=(),
    )
    done = runner(step, host, timeout_sec=timeout_sec)
    found = {}
    for line in (done.stdout or "").splitlines():
        stamp = _parse_line(line)
        if stamp is None:
            continue
        # Keyed by the UNIT. Two records of one declaration are two units, and
        # keying by the declaration made the second replace the first, so one
        # of them read as never provisioned while it was running.
        found[str(getattr(stamp, "unit_ref", "") or stamp.workload_id)] = stamp
    return found



def by_unit(stamps) -> dict:
    """Records keyed by the UNIT they describe, so none replaces another.

    Keyed by workload id, two records of one declaration collapse into one and
    the surviving unit's sibling reads as never provisioned. The unit reference
    is what is unique, and every record already carries it.
    """
    out = {}
    for stamp in stamps or ():
        ref = str(getattr(stamp, "unit_ref", "") or "").strip()
        out[ref or str(getattr(stamp, "workload_id", "") or "")] = stamp
    return out


def _parse_line(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return from_json(line)
    except (ValueError, TypeError):
        return None


def _first_stamp(text: str):
    for line in (text or "").splitlines():
        stamp = _parse_line(line)
        if stamp is not None:
            return stamp
    return None
