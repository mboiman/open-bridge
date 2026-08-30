#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Can a session still find what this Bridge has?

    python3 scripts/check-reachability.py             # contract + scenarios
    python3 scripts/check-reachability.py --families  # what was discovered
    python3 scripts/check-reachability.py --mutate    # prove the check bites

The context budget proves the always-on layer is SMALL. The context index
proves a body can be FETCHED. Neither proves the thing both exist for: that a
session still knows the body is there. A cut that removes the last mention of
`infra/channels/` leaves every other guard green — the budget is happier, every
remaining route resolves, every card round-trips — and the capability is gone,
because nothing will ever go looking for it.

TWO CHECKS, and the second is the one a person would call a simulation.

THE CONTRACT. Every config family the tree actually has is named, BY ITS PATH,
somewhere in the always-on surface. Derived from the tree, never from a list:
a list written once is wrong later, and wrong in the direction that hides the
newest thing. By path and not by prose, because "the machine inventory" is a
name and `infra/remotes/` is a route — only the second can be followed, and
only the second breaks loudly when the directory moves.

THE SCENARIOS. One per thing a person actually asks for, each naming the
vocabulary they would use and the family that has to be reachable from it.
This is the chain the cuts were supposed to preserve, walked end to end:
somebody says "wake the machine", the resident layer carries both that word
and the route, and the route resolves to real content. An instance adds its
own in `reachability-scenarios.yaml` — its machine names, its customers, its
own words.

WHAT COUNTS AS RESIDENT is whatever `measure-context.py` measures, and that is
deliberate: two definitions of always-on would let a pointer live in something
the session never loads. A deferred standing order does not count. Its body
arrives on a trigger, and the trigger is the thing that went missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a documented dependency
    yaml = None

HERE = Path(__file__).resolve().parent

# The cluster wrappers. A subdirectory holding at least one YAML is a config
# type; a directory of prose is documentation and not this check's business.
WRAPPERS = ("identity", "infra", "workflow")

# Families that are not under a wrapper and have their own lifecycle. Only
# demanded when they exist: a fresh clone has no `.claude/agents/`, and failing
# for its absence would be a check about someone else's tree.
TOP_LEVEL = (
    "rules/",
    "protocols/standing-orders/",
    "skills/",
    "trackers/",
    "themes/",
    ".claude/agents/",
    "work/",
)

# CORE scenarios: generic on purpose, because CORE cannot know a machine's name
# or a customer's. Each is (what somebody asks, the words they would use, the
# family that has to be reachable). An instance adds its real ones in
# `reachability-scenarios.yaml`.
SCENARIOS = [
    ("repair or wake a machine", ["remote"], "infra/remotes/"),
    ("send a message somewhere", ["channel"], "infra/channels/"),
    ("who receives an outgoing message", ["mandant"], "identity/mandants/"),
    ("which identity am I sending as", ["persona"], "identity/personas/"),
    ("a cloud login or a vault", ["accounts"], "identity/accounts/"),
    ("move an item on a board", ["projects"], "workflow/projects/"),
    ("where does this get documented", ["contexts"], "workflow/contexts/"),
    ("something that runs on a schedule", ["workload"], "workflow/workloads/"),
    ("is the backup healthy", ["backups"], "infra/backups/"),
    ("put it in the calendar", ["calendar"], "workflow/calendars/"),
    ("what rule applies here", ["rules/"], "rules/"),
]

SCENARIO_FILE = "reachability-scenarios.yaml"


def _meter():
    """`measure-context.py` by path — one definition of the always-on surface."""
    spec = importlib.util.spec_from_file_location(
        "measure_context", HERE / "measure-context.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------- discovery --


def discover_families(repo_root) -> list[str]:
    """Every config family in this tree, as repo-relative directory paths."""
    root = Path(repo_root)
    found: list[str] = []

    for wrapper in WRAPPERS:
        base = root / wrapper
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # A template or a schema is enough. A family CORE ships with no
            # instance yet is a shipped capability, and a capability nobody can
            # discover is exactly what this checks.
            if any(child.glob("*.yaml")) or any(child.glob("*.yml")):
                found.append(f"{wrapper}/{child.name}/")

    for rel in TOP_LEVEL:
        if (root / rel).is_dir():
            found.append(rel)

    return found


def load_scenarios(repo_root) -> list[tuple[str, list[str], str]]:
    """CORE scenarios plus this instance's own, when it declares any."""
    scenarios = list(SCENARIOS)
    path = Path(repo_root) / SCENARIO_FILE
    if yaml is None or not path.is_file():
        return scenarios
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in data.get("scenarios") or []:
        scenarios.append(
            (
                str(entry.get("asks", "")),
                [str(v) for v in (entry.get("vocabulary") or [])],
                str(entry.get("family", "")),
            )
        )
    return scenarios


# ---------------------------------------------------------------- checks --


def surface_of(repo_root) -> str:
    meter = _meter()
    return meter.always_on_text(repo_root, meter.load_budget(repo_root))


def check(repo_root, surface: str | None = None) -> list[str]:
    """Families the always-on surface does not name."""
    surface = surface_of(repo_root) if surface is None else surface
    return [
        f"{family}: exists in the tree and is named nowhere a session loads"
        for family in discover_families(repo_root)
        if family not in surface
    ]


def check_scenarios(repo_root, surface: str | None = None) -> list[str]:
    """Walk each scenario end to end: vocabulary, route, content."""
    root = Path(repo_root)
    surface = surface_of(repo_root) if surface is None else surface
    findings = []

    for asks, vocabulary, family in load_scenarios(repo_root):
        target = root / family
        if not target.exists():
            # Not every instance has every family, and demanding one it never
            # enabled would be a check about a Bridge that does not exist.
            continue
        missing = [word for word in vocabulary if word.lower() not in surface.lower()]
        if missing:
            findings.append(
                f"{family}: '{asks}' — the words {missing} are in no loaded file, "
                f"so nothing would trigger the lookup"
            )
        if family not in surface:
            findings.append(
                f"{family}: '{asks}' — the vocabulary is resident and the route "
                f"is not, so a session would know to look and not where"
            )
        if not any(target.glob("*.yaml")) and not any(target.glob("*.md")):
            findings.append(
                f"{family}: '{asks}' — the route is resident and resolves to an "
                f"empty directory"
            )
    return findings


def mutate(repo_root) -> list[str]:
    """Prove the contract BITES: hide each pointer in turn, expect a finding.

    A guard nobody has watched fail is a guard nobody knows the shape of. This
    is the same needle discipline the workload and remote suites use, and it
    has already caught two hollow needles elsewhere in this repo.
    """
    real = surface_of(repo_root)
    dead = []
    for family in discover_families(repo_root):
        if family not in real:
            continue  # already a live finding; the contract check reports it
        hidden = real.replace(family, "«removed»")
        if not any(family in f for f in check(repo_root, surface=hidden)):
            dead.append(
                f"{family}: removing every mention of it produced no finding — "
                f"this needle is hollow and proves nothing"
            )
    return dead


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo-root", default=str(HERE.parent))
    parser.add_argument("--families", action="store_true", help="list what was found")
    parser.add_argument("--mutate", action="store_true", help="prove the check bites")
    args = parser.parse_args(argv)

    root = Path(args.repo_root)

    if args.families:
        for family in discover_families(root):
            print(family)
        return 0

    if args.mutate:
        dead = mutate(root)
        for finding in dead:
            print(finding, file=sys.stderr)
        if dead:
            return 1
        print("check-reachability: every needle bites")
        return 0

    findings = check(root) + check_scenarios(root)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"check-reachability: {len(findings)} finding(s)", file=sys.stderr)
        return 1

    families = discover_families(root)
    scenarios = [s for s in load_scenarios(root) if (root / s[2]).exists()]
    print(
        f"check-reachability: {len(families)} config famil"
        f"{'y' if len(families) == 1 else 'ies'} reachable, "
        f"{len(scenarios)} scenario(s) walked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
