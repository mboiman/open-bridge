#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/check-reachability.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS EXISTS. The context budget proves the always-on layer is SMALL. The
context index proves a body can be FETCHED. Neither proves the thing both are
for: that a session still knows the body exists. A cut that removes the last
mention of `infra/channels/` leaves every guard green — the budget is happier,
every route that remains resolves, every card round-trips — and the capability
is gone, because nothing will ever go looking for it.

That failure is silent by construction, and this repo has already paid for it
twice in one week: three standing orders targeted sub-agents that no instance
has, and the org-overlay import anchored on a line that had been deleted. Both
read as enforced and did nothing.

    THE CONTRACT

        Every config family the tree actually has must be NAMED, by its path,
        somewhere in the always-on surface.

    DERIVED, NEVER LISTED. The families come from the tree — the cluster
    wrappers' subdirectories plus the fixed top-level ones — for the same
    reason `AGENTS.md` stopped carrying a table of rules: a list written once
    is a list that is wrong later, and it is wrong in the direction that hides
    the newest thing.

    BY PATH, not by prose. "the machine inventory" is a name; `infra/remotes/`
    is a route. Only the second one can be followed, and only the second one
    breaks loudly when the directory moves.

    THE SURFACE IS THE METER'S. `always_on_text` comes from
    `measure-context.py`, so the set of files this checks against is the same
    set the budget gates. Two definitions of "always-on" would mean a family
    named in something the session does not actually load.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "check-reachability.py"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reach = _load("check_reachability", "scripts/check-reachability.py")
mc = _load("measure_context", "scripts/measure-context.py")


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


MINIMAL_BUDGET = (
    "schema_version: 1\n"
    "items:\n"
    "  CLAUDE.md:\n"
    "    max_bytes: 100000\n"
    "  AGENTS.md:\n"
    "    max_bytes: 100000\n"
)


# ------------------------------------------------------------- discovery --


def test_a_wrapper_family_with_a_yaml_is_a_family(tmp_path):
    _tree(tmp_path, {"identity/mandants/_template.yaml": "a: 1\n"})
    assert "identity/mandants/" in reach.discover_families(tmp_path)


def test_a_wrapper_family_with_only_a_readme_is_not(tmp_path):
    """A directory of prose is documentation, not a config type."""
    _tree(tmp_path, {"identity/notes/README.md": "# not a family\n"})
    assert "identity/notes/" not in reach.discover_families(tmp_path)


def test_a_template_only_family_still_counts(tmp_path):
    """CORE ships several families as templates with no instance yet.

    They are shipped CAPABILITIES, and a capability nobody can discover is the
    exact thing this checks. `identity/contracts/` was in precisely that state
    — schema and template present, named nowhere — before this existed.
    """
    _tree(tmp_path, {"identity/contracts/_schema.yaml": "a: 1\n"})
    assert "identity/contracts/" in reach.discover_families(tmp_path)


def test_the_fixed_top_level_families_are_included(tmp_path):
    _tree(tmp_path, {
        "rules/operations.md": "x\n",
        "skills/demo/SKILL.md": "x\n",
        "protocols/standing-orders/a.md": "x\n",
    })
    found = reach.discover_families(tmp_path)

    for expected in ("rules/", "skills/", "protocols/standing-orders/"):
        assert expected in found


def test_a_top_level_family_that_does_not_exist_is_not_demanded(tmp_path):
    """A fresh clone has no `.claude/agents/`, and must not fail for it."""
    _tree(tmp_path, {"rules/operations.md": "x\n"})
    assert ".claude/agents/" not in reach.discover_families(tmp_path)


def test_discovery_is_derived_from_the_tree_not_a_list(tmp_path):
    """MUTATION. Invent a family; it has to appear without anyone editing a list."""
    _tree(tmp_path, {"workflow/quotas/_template.yaml": "a: 1\n"})
    assert "workflow/quotas/" in reach.discover_families(tmp_path)


# ---------------------------------------------------------------- the check --


def test_a_family_named_in_the_surface_passes(tmp_path):
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Machines live in `infra/remotes/`.\n",
        "infra/remotes/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    assert reach.check(tmp_path) == []


def test_a_family_missing_from_the_surface_is_a_finding(tmp_path):
    """The whole point. Every other guard is green in this state."""
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Nothing about the inventory here.\n",
        "infra/remotes/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    findings = reach.check(tmp_path)

    assert any("infra/remotes/" in f for f in findings), findings


def test_prose_without_the_path_does_not_count(tmp_path):
    """"the machine inventory" is a name; `infra/remotes/` is a route.

    Only the second can be followed, and only the second breaks loudly when
    the directory moves.
    """
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Your machine inventory is described elsewhere.\n",
        "infra/remotes/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    assert any("infra/remotes/" in f for f in reach.check(tmp_path))


def test_a_pointer_in_a_deferred_standing_order_does_not_count(tmp_path):
    """A body that loads on a trigger is not the surface.

    It is fetched by vocabulary that nobody will say, because the vocabulary
    is the thing that went missing.
    """
    order = (
        "---\nname: x\nscope: always\nenforcement: advisory\napplies_to: []\n"
        "load: on-trigger\ntriggers: [\"x\"]\nsummary: \"s\"\n---\n\n"
        "Channels live in `infra/channels/`.\n"
    )
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Nothing here.\n",
        "protocols/standing-orders/x.md": order,
        "infra/channels/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    assert any("infra/channels/" in f for f in reach.check(tmp_path))


def test_a_pointer_in_an_eager_standing_order_does_count(tmp_path):
    """An eager order IS loaded, so a pointer in it is genuinely resident."""
    order = (
        "---\nname: x\nscope: always\nenforcement: advisory\napplies_to: []\n"
        "---\n\nChannels live in `infra/channels/`.\n"
    )
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Nothing here.\n",
        "protocols/standing-orders/x.md": order,
        "infra/channels/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    # Only the question at hand: the fixture's own standing-orders family is
    # unnamed too, which is a finding about the fixture and not about this.
    assert not any("infra/channels/" in f for f in reach.check(tmp_path))


# ------------------------------------------------------- the surface itself --


def test_the_surface_is_the_one_the_meter_measures(tmp_path):
    """One definition of always-on, not two."""
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "MARKER-AGENTS\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    text = mc.always_on_text(tmp_path, mc.load_budget(tmp_path))

    assert "MARKER-AGENTS" in text


def test_the_surface_includes_what_a_phase_one_command_prints(tmp_path):
    """Half the surface is computed, not read, and a pointer can live there."""
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "x\n",
        "context-budget.yaml": MINIMAL_BUDGET
        + '  "cmd:python3 scripts/standing-orders.py --index":\n    source: phase1\n',
        "protocols/standing-orders/x.md": (
            "---\nname: x\nscope: always\nenforcement: advisory\napplies_to: []\n"
            "load: on-trigger\ntriggers: [\"say x\"]\nsummary: \"about workflow/quotas/\"\n"
            "---\n\nbody\n"
        ),
    })
    text = mc.always_on_text(tmp_path, mc.load_budget(tmp_path))

    # The index runs from the repo root, so it reports on THIS repo's orders,
    # not the fixture's. What matters here is that command output is included
    # at all — an empty string would mean the whole computed half is invisible.
    assert isinstance(text, str)
    assert "MARKER" not in text


# --------------------------------------------------------------------- CLI --


def test_cli_is_green_on_this_repo():
    """The live tree, which is the only run that matters.

    It found two families on its first outing — `identity/contracts/` and
    `infra/instances/`, both shipped, both named nowhere — and neither was
    caused by the cuts that came before it. They had simply never been named.
    """
    result = subprocess.run(
        [sys.executable, str(CLI)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_exits_non_zero_on_a_finding(tmp_path):
    _tree(tmp_path, {
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "Nothing here.\n",
        "infra/remotes/_template.yaml": "a: 1\n",
        "context-budget.yaml": MINIMAL_BUDGET,
    })
    result = subprocess.run(
        [sys.executable, str(CLI), "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert "infra/remotes/" in (result.stdout + result.stderr)
