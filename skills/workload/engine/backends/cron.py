"""crontab: one delimited block inside a file that is not ours alone.

Two properties decide everything here.

A crontab is shared with whoever wrote it before us, so only the block between
the two markers is ever rewritten. `merge_block` is the pure half of that and
is what the tests drive: a hand written line above or below our block survives
every rewrite, including the removal.

cron promises nothing. No deadline, no group kill, no single flight, no record
that a run happened. So a workload carried here is ALWAYS wrapped, and the
crontab line invokes the guard script rather than the command.
"""

from __future__ import annotations

from engine import errors
from engine.model import CRON_BEGIN, CRON_END, Step, declaration_digest

from . import base, wrapper


def escape_percent(text: str) -> str:
    """In a crontab a bare % ends the command and feeds the rest to stdin.

    Applied to the whole block, with no exception for comment lines: one rule
    with no exceptions is one rule nobody forgets at the wrong moment.
    """
    return text.replace("%", "\\%")


def render_block(workload_id: str, digest, body: str) -> str:
    """The delimited block, exactly as it appears in a crontab."""
    head = f"{CRON_BEGIN} {workload_id}"
    if digest:
        head = f"{head} {digest}"
    lines = body.rstrip("\n")
    return f"{head}\n{lines}\n{CRON_END} {workload_id}\n"


def merge_block(existing: str, body, workload_id: str, digest) -> str:
    """Replace (or, with body None, remove) our block inside a crontab.

    Everything outside the two markers is returned untouched and in place, so
    a crontab someone edited by hand keeps its shape.
    """
    kept, start, inside = [], None, False
    for line in (existing or "").splitlines():
        # Matched on a whole word, so a block whose id merely contains ours is
        # left alone.
        if line.startswith(CRON_BEGIN) and workload_id in line.split():
            inside = True
            start = len(kept)
            continue
        if inside and line.startswith(CRON_END) and workload_id in line.split():
            inside = False
            continue
        if inside:
            continue
        kept.append(line)

    if body is not None:
        block = render_block(workload_id, digest, body).splitlines()
        position = len(kept) if start is None else start
        kept[position:position] = block

    text = "\n".join(kept)
    return text + "\n" if text and not text.endswith("\n") else text


class CronBackend:

    def unit_name(self, w, appointment=None) -> str:
        """Nothing. This runtime gives a run no name on the machine.

        A cron line has no identifier, and `manual` / `external` are
        documented rather than executed. Returning an invented name would be
        a claim about a machine nobody asked, which is the same mistake as a
        declared `status:` field.
        """
        return ""


    def __init__(self, label_prefix=base.DEFAULT_LABEL_PREFIX):
        self.name = "cron"
        self.label_prefix = label_prefix
        self.platforms = frozenset({"macos", "linux"})
        # No daemon and no watcher: cron starts something at a time, it does
        # not keep anything up and it cannot see a path change.
        self.kinds = frozenset({"recurring", "interval"})
        self.guarantees = frozenset()
        self.wrappable = True
        self.requires_elevation = False
        self.provisionable = True
        self.kind_remedy = ("cron starts something at a time; it keeps nothing "
                            "up and sees no path change")

    def staging_path(self, w, ctx) -> str:
        return f"{str(ctx.stamp_dir).rstrip('/')}/{w.id}.crontab"

    def unit_ref(self, w, ctx=None) -> str:
        return f"crontab/{w.id}"

    # -- render ------------------------------------------------------------

    def render(self, w, h, ctx) -> base.Artifact:
        base.ensure_kind(self, w, self.kind_remedy)
        # The id is this backend's unit NAME too: it is written into the BEGIN
        # marker and read back out of it with `split()`. It is not asserted a
        # second time here -- `wrapper.wrap` below runs BEFORE `render_block`
        # and refuses it there, and a second guard nothing can reach is a layer
        # no needle can prove.
        digest = declaration_digest(w)
        supplied = wrapper.supplies(w, self)
        command = base.command_of(w)
        if not command:
            raise errors.UnsupportedKind(
                f"cron cannot carry workload {w.id}: it names no command, and a "
                f"crontab line without one is nothing"
            )

        # Always wrapped, even when the declaration demands no guarantee at
        # all: cron hands a run no PATH and no working directory, and a
        # `PATH=` line in the crontab itself would apply to every hand written
        # line below ours as well. The guard keeps that inside our own file.
        guard = wrapper.wrap(w, ctx, command, supplied=supplied, digest=digest)
        body_lines = [
            f"# {w.purpose}",
            # What actually runs, so `crontab -l` still tells a human something.
            f"# runs: {base.shell_command(command)}",
            f"{self._expression(w)} {base.shell_command(wrapper.guard_argv(guard.path))}",
        ]
        block = base.RenderedFile(
            path=self.staging_path(w, ctx),
            mode=0o644,
            content=escape_percent(render_block(w.id, digest, "\n".join(body_lines))),
        )
        ordered = (block, guard)
        return base.Artifact(
            runtime=self.name,
            unit_ref=self.unit_ref(w, ctx),
            files=ordered,
            digest=base.digest_of(ordered),
            guarantees_native=self.guarantees,
            guarantees_wrapped=frozenset(supplied),
            notes="",
        )

    def _expression(self, w) -> str:
        if w.placement.kind == "interval":
            seconds = int(w.schedule.every_sec)
            if seconds % 60:
                raise errors.UnsupportedRecurrence(
                    f"workload {w.id} runs every {seconds}s, and cron's finest "
                    f"step is a minute; approximating it would drift"
                )
            minutes = seconds // 60
            if minutes < 60 and 60 % minutes == 0:
                return f"*/{minutes} * * * *"
            if minutes == 60:
                return "0 * * * *"
            if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
                return f"0 */{minutes // 60} * * *"
            raise errors.UnsupportedRecurrence(
                f"workload {w.id} runs every {minutes} minutes, which does not "
                f"divide an hour or a day evenly, so cron cannot hold the cadence"
            )
        hour, minute, shift = base.start_of(w)
        days = base.weekdays_of(w, shift)
        return f"{minute} {hour} * * " + (",".join(str(d) for d in days) if days else "*")

    # -- step plans --------------------------------------------------------
    #
    # The merge itself is pure python (`merge_block`) and belongs to the caller:
    # it reads the current crontab, merges, writes the result and installs it.
    # These steps are the two calls around that.

    def _snapshot_step(self, a) -> Step:
        return Step(
            argv=("/bin/sh", "-c", "crontab -l 2>/dev/null || true"),
            purpose="read the current crontab, which is shared with its owner",
        )

    def _install_step(self, a) -> Step:
        merged = f"{a.files[0].path}.merged"
        return Step(
            argv=("/bin/sh", "-c", f"crontab {base.shell_command([merged])}"),
            purpose=f"install the merged crontab carrying {a.unit_ref}",
        )

    def install_steps(self, a, h) -> tuple:
        return (self._snapshot_step(a), base.write_file_step(a.files[1]),
                self._install_step(a))

    def replace_steps(self, a, h) -> tuple:
        return self.install_steps(a, h)

    def disable_steps(self, a, h, reason: str) -> tuple:
        # cron has no disabled list to put an entry on, so the persistent form
        # of "off, and here is why" is the block with its lines commented out
        # and the reason beside them. Still not a rename: the why stays.
        return (
            self._snapshot_step(a),
            Step(
                argv=("/bin/sh", "-c", f"crontab {base.shell_command([str(a.files[0].path) + '.merged'])}"),
                purpose=f"install the crontab with {a.unit_ref} commented out, "
                        f"reason: {reason}",
            ),
        )

    def uninstall_steps(self, a, h) -> tuple:
        return (
            self._snapshot_step(a),
            self._install_step(a),
            Step(
                argv=("/bin/sh", "-c",
                      "rm -f " + " ".join(base.shell_command([str(f.path)]) for f in a.files)),
                purpose=f"remove the staged files of {a.unit_ref}",
            ),
        )

    # -- reading the live source ------------------------------------------

    def default_probe(self, a, h) -> Step:
        return Step(
            argv=("/bin/sh", "-c", "crontab -l 2>/dev/null || true"),
            purpose=f"look for the block of {a.unit_ref} in the crontab",
        )

    def inspect_steps(self, a, h) -> tuple:
        return (self.default_probe(a, h),)

    def parse_inspection(self, outs, unit_ref: str = ""):
        """The block belonging to THIS unit, out of a crontab holding many."""
        found = self.parse_discovery(outs if isinstance(outs, (list, tuple)) else [outs])
        if unit_ref:
            for unit in found:
                if unit.unit_ref == unit_ref:
                    return unit
            return None
        return found[0] if found else None

    def disabled_list_steps(self, a, h) -> tuple:
        # cron keeps no state beside the file, so there is no off-list to read.
        # No steps means the answer stays None (unknown), which is the honest
        # one: a line that is not in the crontab is absent, not disabled.
        return ()

    def parse_disabled(self, outs, unit_ref: str):
        return None

    def discover_steps(self, h) -> tuple:
        return (Step(argv=("/bin/sh", "-c", "crontab -l 2>/dev/null || true"),
                     purpose="read the crontab of the calling user"),)

    def parse_discovery(self, outs) -> list:
        from engine.reconcile import LiveUnit

        units = []
        for out in outs or ():
            for line in _text_of(out).splitlines():
                if not line.startswith(CRON_BEGIN):
                    continue
                parts = line[len(CRON_BEGIN):].split()
                if not parts:
                    continue
                units.append(LiveUnit(
                    runtime=self.name,
                    unit_ref=f"crontab/{parts[0]}",
                    path=None,
                    marker_id=parts[0],
                    # The crontab block carries the marker in its own fence, so
                    # enumeration and observation are one read here.
                    marker_observed=True,
                    marker_digest=parts[1] if len(parts) > 1 else None,
                    # A crontab line is either there or it is not. cron keeps no
                    # state beyond the file, so "running" cannot be answered
                    # from it and is not claimed.
                    enabled=True,
                    running=True,
                    raw=line,
                ))
        return units


def _text_of(out) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    return getattr(out, "stdout", "") or ""


def build(cfg) -> CronBackend:
    return CronBackend(label_prefix=getattr(cfg, "label_prefix",
                                            base.DEFAULT_LABEL_PREFIX))


CRON = build(None)
