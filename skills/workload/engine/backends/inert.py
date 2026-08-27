"""manual and external: declared so they are visible, never provisioned.

They are backends like any other, which is the whole point. A run started by
hand in a terminal multiplexer, or one a third-party platform manages, still
belongs in the inventory: `reconcile` has to see it, and the two kinds that can
never be created from a declaration must not become an `if` in the middle of
the state machine. They are data here instead.

`render` refuses. `default_probe` and `discover_steps` keep working, so a run
nobody provisions can still be observed.
"""

from __future__ import annotations

from engine import errors
from engine.model import Step


class InertBackend:

    def unit_name(self, w, appointment=None) -> str:
        """Nothing. This runtime gives a run no name on the machine.

        A cron line has no identifier, and `manual` / `external` are
        documented rather than executed. Returning an invented name would be
        a claim about a machine nobody asked, which is the same mistake as a
        declared `status:` field.
        """
        return ""


    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        # Every platform: what carries these runs is not a service manager, so
        # no platform excludes them.
        self.platforms = frozenset({"macos", "linux", "windows"})
        self.kinds = frozenset({"recurring", "interval", "daemon", "watch",
                                "agent", "oneshot"})
        self.guarantees = frozenset()
        self.wrappable = False
        self.requires_elevation = False
        self.provisionable = False
        self.kind_remedy = ""

    def render(self, w, h, ctx):
        raise errors.NotProvisionable(
            f"{w.id} names runtime {self.name}: {self.reason}. The declaration "
            f"exists to make the run visible, not to create it, so nothing is "
            f"rendered and nothing is installed."
        )

    def install_steps(self, a, h) -> tuple:
        return ()

    def replace_steps(self, a, h) -> tuple:
        return ()

    def disable_steps(self, a, h, reason: str) -> tuple:
        return ()

    def uninstall_steps(self, a, h) -> tuple:
        return ()

    def default_probe(self, a, h) -> Step:
        # Nothing here owns a live source of its own. Whatever proof exists is
        # the declaration's own probe, and its absence is reported as unknown
        # rather than answered with a guess.
        return Step(
            argv=("/bin/sh", "-c", "printf %s unknown"),
            purpose=f"{self.name} runs carry no default probe: "
                    f"the declaration's own probe is the only source",
        )

    def inspect_steps(self, a, h) -> tuple:
        return ()

    def parse_inspection(self, outs, unit_ref: str = ""):
        return None

    def disabled_list_steps(self, a, h) -> tuple:
        return ()

    def parse_disabled(self, outs, unit_ref: str):
        return None

    def discover_steps(self, h) -> tuple:
        return ()

    def parse_discovery(self, outs) -> list:
        return []


MANUAL = InertBackend(
    "manual",
    "a person starts it by hand and no unit file exists",
)
EXTERNAL = InertBackend(
    "external",
    "a third-party platform manages it and it is never touched from here",
)


def build(cfg) -> tuple:
    return MANUAL, EXTERNAL
