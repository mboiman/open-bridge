"""The Bridge's own dispatcher: one entry in a registry, not a unit file.

What it guarantees is read FROM configuration and defaults to nothing. Today's
dispatcher supplies neither a deadline nor a group kill, so a workload that
demands either lands in the degraded-backend refusal instead of being carried
badly. When the dispatcher grows those properties, configuration says so and no
source file here changes.

It is also the one backend a guard script cannot help: a registry entry is data
read by something else, not a command line of ours to wrap.
"""

from __future__ import annotations

from engine import errors
from engine.model import MARKER_ENV_ID, Guarantee, Step, declaration_digest

from . import base


class DispatcherBackend:

    def unit_name(self, w, appointment=None) -> str:
        """Nothing. This runtime gives a run no name on the machine.

        A cron line has no identifier, and `manual` / `external` are
        documented rather than executed. Returning an invented name would be
        a claim about a machine nobody asked, which is the same mistake as a
        declared `status:` field.
        """
        return ""


    def __init__(self, registry=None, guarantees=(), label_prefix=base.DEFAULT_LABEL_PREFIX):
        self.name = "dispatcher"
        self.registry = registry
        self.label_prefix = label_prefix
        self.platforms = frozenset({"macos", "linux"})
        # It polls, so it can hold appointments, cadences and a single date. It
        # keeps nothing up and watches no path.
        self.kinds = frozenset({"recurring", "interval", "oneshot"})
        self.guarantees = frozenset(guarantees)
        # A registry entry has no process around which a guard could sit.
        self.wrappable = False
        self.requires_elevation = False
        self.provisionable = True
        self.kind_remedy = ("the dispatcher polls; it keeps nothing up and "
                            "watches no path")

    def unit_ref(self, w, ctx=None) -> str:
        return f"dispatcher/{w.id}"

    def render(self, w, h, ctx) -> base.Artifact:
        base.ensure_kind(self, w, self.kind_remedy)
        registry = ctx.dispatcher_registry or self.registry
        if not registry:
            raise errors.DispatcherNotConfigured(
                "no dispatcher_registry is configured, so there is nowhere to "
                f"put the entry for {w.id}; set workloads.dispatcher_registry "
                "in the bridge configuration"
            )
        digest = declaration_digest(w)
        entry = base.RenderedFile(
            path=str(registry),
            mode=0o644,
            content=self._entry(w, digest),
        )
        return base.Artifact(
            runtime=self.name,
            unit_ref=self.unit_ref(w, ctx),
            files=(entry,),
            digest=base.digest_of((entry,)),
            guarantees_native=self.guarantees,
            guarantees_wrapped=frozenset(),
            notes="one entry of the registry, merged into it by the caller "
                  "rather than written over it",
        )

    def _entry(self, w, digest) -> str:
        marker = MARKER_ENV_ID.lower()
        # Named by id, never by the path this checkout happens to sit at: the
        # bytes have to be the same on every machine, or every render looks
        # like drift.
        lines = [
            f"# Entry rendered from the declaration {w.id}.yaml. Merged into the",
            "# registry, never written over it: the registry carries every run.",
            f"- {marker}: {w.id}",
            f"  {marker}_digest: \"{digest}\"",
            f"  kind: {w.placement.kind}",
        ]
        schedule = getattr(w, "schedule", None)
        for field in ("rrule", "every_sec", "at", "delivery_at",
                      "duration_estimate_min", "timezone"):
            value = getattr(schedule, field, None) if schedule else None
            if value is None or value == "" or value == ():
                continue
            lines.append(f"  {field}: {value}" if isinstance(value, int)
                         else f"  {field}: \"{value}\"")
        command = base.command_of(w)
        if command:
            lines.append("  command: [" + ", ".join(f'"{a}"' for a in command) + "]")
        deadline = base.timeout_of(w)
        if deadline:
            lines.append(f"  timeout_sec: {int(deadline)}")
        return "\n".join(lines) + "\n"

    # -- step plans --------------------------------------------------------
    #
    # Provisioning here is a merge into a registry the dispatcher owns, which
    # the caller performs with the entry above. There is nothing to load, start
    # or bootstrap, so no step plan claims otherwise.

    def install_steps(self, a, h) -> tuple:
        return ()

    def replace_steps(self, a, h) -> tuple:
        return ()

    def disable_steps(self, a, h, reason: str) -> tuple:
        return ()

    def uninstall_steps(self, a, h) -> tuple:
        return ()

    def default_probe(self, a, h) -> Step:
        return Step(
            argv=("/bin/sh", "-c",
                  f"grep -F {base.shell_command([a.unit_ref.split('/', 1)[-1]])} "
                  f"{base.shell_command([str(self.registry or '')])} || true"),
            purpose=f"look for {a.unit_ref} in the dispatcher registry",
        )

    def inspect_steps(self, a, h) -> tuple:
        return (self.default_probe(a, h),)

    def parse_inspection(self, outs, unit_ref: str = ""):
        """The registry line for this entry, or None when it is not in there."""
        text = base.text_of(outs[0] if isinstance(outs, (list, tuple)) else outs)
        label = base.label_of(unit_ref)
        if not text.strip() or (label and label not in text):
            return None
        from engine.reconcile import LiveUnit

        return LiveUnit(
            runtime=self.name,
            unit_ref=unit_ref,
            path=str(self.registry or "") or None,
            marker_id=label or None,
            # The registry IS the file that carries the marker: enumerating it
            # and reading the marker are the same read.
            marker_observed=True,
            marker_digest=_digest_in(text),
            # A registry entry is a declaration of intent held by somebody
            # else's scheduler. Whether it is switched off, and whether it ran,
            # are not questions a grep of that file can answer.
            enabled=None,
            running=None,
            raw=text.strip(),
        )

    def disabled_list_steps(self, a, h) -> tuple:
        return ()

    def parse_disabled(self, outs, unit_ref: str):
        return None

    def discover_steps(self, h) -> tuple:
        return ()

    def parse_discovery(self, outs) -> list:
        return []


def _digest_in(text: str):
    """The first `sha256:` value in a registry line, when it carries one."""
    for word in text.replace('"', " ").replace("'", " ").split():
        if word.startswith("sha256:"):
            return word
    return None


def build(cfg) -> DispatcherBackend:
    """Its capabilities come from configuration, never from this file."""
    raw = getattr(cfg, "dispatcher_guarantees", ()) or ()
    return DispatcherBackend(
        registry=getattr(cfg, "dispatcher_registry", None),
        guarantees=frozenset(Guarantee(g) for g in raw),
        label_prefix=getattr(cfg, "label_prefix", base.DEFAULT_LABEL_PREFIX),
    )


DISPATCHER = build(None)
