#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/measure-context.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS SCRIPT EXISTS. Every session loads a fixed body of text before the
first answer: the `@`-imports named in CLAUDE.md, the standing orders carrying
`scope: always`, and the Phase 1 reads. Nothing measures it, so it only ever
grows, and it grows invisibly: half of it never appears in a client's context
listing because it arrives through tool reads rather than imports. One file in
this repo has never sprawled, `identity/agent/SOUL.md`, and the only thing that
distinguishes it is a declared cap that a validator enforces. This script
generalises that cap to the whole always-on surface.

    load_budget(repo_root) -> dict
        Reads `context-budget.yaml`, then overlays `context-budget.user.yaml`
        when present. The overlay replaces an item wholesale by key; it is not
        a deep merge, because a half-overridden cap is worse than either cap.
        A missing CORE budget is a hard error: a meter with no policy enforces
        nothing and would report green forever.

    discover_imports(repo_root, entry="CLAUDE.md", max_depth=4) -> list[str]
        The `@`-imports actually loaded, followed recursively, first-seen order,
        deduplicated, cycle-safe. Only a line that STARTS with `@` counts; an
        address inside prose is not an import. A target that does not resolve is
        RETURNED rather than dropped, so it surfaces as `missing` instead of
        quietly leaving the measured set.

    discover_standing_orders(repo_root) -> list[str]
        The orders that are actually always-on: `scope: always` AND
        `load: eager` (the default). An `on-trigger` order is fetched by its
        vocabulary and is not part of the always-on surface, so counting it
        here would overstate the budget and hide the saving. The load contract
        itself lives in `scripts/lib/standing_orders.py`, shared so the meter
        and the index can never disagree about what is always-on.

    count_tokens(text, method, bytes_per_token, model) -> (int, str)
        Returns the count AND the method that actually produced it. `api` is
        exact, via `messages.count_tokens` on the declared model (counts are
        model-specific, so the model travels with the number too); `bytes` is an
        estimate from a declared calibration. A requested
        `api` that cannot run falls back to `bytes` and says `bytes`. It never
        reports an estimate as exact, which is the whole reason the method
        travels with the number.

    COMMAND ITEMS. Some always-on payload is not a file. Phase 1 loads the
    standing-order index and a slice of `bridge-config.yaml`, both computed at
    the moment of use so they can never go stale. A budget item keyed
    `cmd:<command>` is measured by running it and measuring stdout, which is
    what is actually loaded. Only `python3 scripts/...` is accepted: a budget
    file is reviewed, but it is still config, and config that can run anything
    is a different kind of file than the one anybody reviewed it as.

    collect_rows(repo_root, budget, method) -> list[dict]
        One row per always-on item, sorted by (source, path) so an unchanged
        tree renders identically twice.

    item_state(row) -> str
        ok          measured, under `max_bytes`
        over        measured, above `max_bytes`               -> FAILS
        uncapped    measured, no cap declared                 -> warns only
        undeclared  discovered on disk, absent from budget    -> FAILS
        absent      declared `optional: true`, not on disk    -> ok
        missing     declared and required, not on disk        -> FAILS

        `undeclared` is the load-bearing one. Without it the budget silently
        stops covering the tree the moment somebody adds an import, and a meter
        that cannot see a new file is worse than no meter, because it reports
        green while the thing it watches grows.

    render_report(rows, method, bytes_per_token) -> str
        Deterministic markdown. No timestamp, no host, no absolute path. States
        the method in the header and marks the token column as an estimate
        whenever the method is not exact.

    main(argv) -> int
        0 when no row is in a failing state, 1 otherwise.

EVERY GATE HAS A FIXTURE THAT TRIPS IT. A control that is never made to fail
proves nothing about the rule it claims to enforce; the same reasoning the
remote-inventory battery states in `infra/remotes/_tests/run.sh --mutate`.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "measure-context.py"


def _load_module():
    """Import the hyphenated script by path (it is not an importable name)."""
    spec = importlib.util.spec_from_file_location("measure_context", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure_context"] = mod
    spec.loader.exec_module(mod)
    return mod


mc = _load_module()


# --------------------------------------------------------------- fixtures --

def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _order(name: str, scope: str = "always", load: str | None = None) -> str:
    extra = ""
    if load == "on-trigger":
        extra = 'load: on-trigger\ntriggers: ["x"]\nsummary: "s"\n'
    elif load:
        extra = f"load: {load}\n"
    return (
        "---\n"
        f"name: {name}\n"
        f"scope: {scope}\n"
        "enforcement: advisory\n"
        "applies_to: []\n"
        f"{extra}"
        "---\n\n"
        f"# {name}\n\nbody\n"
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal but realistic always-on surface."""
    _write(tmp_path, "CLAUDE.md", "@AGENTS.md\n\n@ecosystem.yaml\n\nprose\n")
    _write(tmp_path, "AGENTS.md", "A" * 500)
    _write(tmp_path, "protocols/standing-orders/task-sync.md", _order("task-sync"))
    _write(tmp_path, "protocols/standing-orders/README.md", "# not an order\n")
    _write(tmp_path, "protocols/standing-orders/_template.md", _order("template"))
    _write(tmp_path, "work/board.md", "B" * 300)
    _write(
        tmp_path,
        "context-budget.yaml",
        "schema_version: 1\n"
        "calibration:\n"
        "  bytes_per_token: 2.4\n"
        "items:\n"
        "  CLAUDE.md:\n"
        "    max_bytes: 1000\n"
        "  AGENTS.md:\n"
        "    max_bytes: 1000\n"
        "  ecosystem.yaml:\n"
        "    optional: true\n"
        "  protocols/standing-orders/task-sync.md:\n"
        "    max_bytes: 1000\n"
        "  work/board.md:\n"
        "    source: phase1\n"
        "    max_bytes: 1000\n",
    )
    return tmp_path


# ------------------------------------------------------- discover_imports --

def test_imports_are_found_at_line_start(tree):
    assert mc.discover_imports(tree) == ["AGENTS.md", "ecosystem.yaml"]


def test_the_entry_file_is_itself_measured(tree):
    """CLAUDE.md is loaded every session; a meter blind at its own front door
    would miss the one file every instance is guaranteed to have."""
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    assert any(r["path"] == "CLAUDE.md" for r in rows)


def test_an_at_sign_inside_prose_is_not_an_import(tmp_path):
    _write(tmp_path, "CLAUDE.md", "write to someone@example.com about @notanimport.md\n")
    assert mc.discover_imports(tmp_path) == []


def test_imports_are_followed_recursively(tmp_path):
    _write(tmp_path, "CLAUDE.md", "@AGENTS.md\n")
    _write(tmp_path, "AGENTS.md", "@docs/deep.md\n")
    _write(tmp_path, "docs/deep.md", "leaf\n")
    assert mc.discover_imports(tmp_path) == ["AGENTS.md", "docs/deep.md"]


def test_an_import_cycle_terminates(tmp_path):
    _write(tmp_path, "CLAUDE.md", "@a.md\n")
    _write(tmp_path, "a.md", "@b.md\n")
    _write(tmp_path, "b.md", "@a.md\n")
    assert mc.discover_imports(tmp_path) == ["a.md", "b.md"]


def test_an_unresolvable_import_is_returned_not_dropped(tmp_path):
    """A blind spot is the failure this whole script exists to prevent."""
    _write(tmp_path, "CLAUDE.md", "@gone.md\n")
    assert mc.discover_imports(tmp_path) == ["gone.md"]


def test_a_repeated_import_appears_once(tmp_path):
    _write(tmp_path, "CLAUDE.md", "@a.md\n@a.md\n")
    _write(tmp_path, "a.md", "x\n")
    assert mc.discover_imports(tmp_path) == ["a.md"]


def test_no_claude_md_yields_no_imports(tmp_path):
    assert mc.discover_imports(tmp_path) == []


# ----------------------------------------------- discover_standing_orders --

def test_only_scope_always_orders_are_collected(tree):
    _write(
        tree,
        "protocols/standing-orders/sometimes.md",
        _order("sometimes", scope="instance"),
    )
    assert mc.discover_standing_orders(tree) == [
        "protocols/standing-orders/task-sync.md"
    ]


def test_readme_and_underscore_files_are_not_orders(tree):
    found = mc.discover_standing_orders(tree)
    assert not any("README" in p or "_template" in p for p in found)


def test_an_on_trigger_order_is_not_part_of_the_always_on_set(tree):
    """It is fetched by its vocabulary, so counting it would overstate the
    budget and hide the very saving the contract buys."""
    _write(
        tree,
        "protocols/standing-orders/deferred.md",
        _order("deferred", load="on-trigger"),
    )
    assert not any(
        "deferred" in p for p in mc.discover_standing_orders(tree)
    )


def test_the_report_says_what_was_deferred_rather_than_dropping_it(tree):
    """Silence about a deferred body reads as a file that vanished."""
    out = mc.render_report([], "bytes", 2.4, deferred=[("a.md", 10), ("b.md", 20)])
    assert "2" in out
    assert "trigger" in out.lower()


def test_user_tier_orders_are_collected_and_sorted(tree):
    _write(tree, "protocols/standing-orders/user/zeta.md", _order("zeta"))
    _write(tree, "protocols/standing-orders/user/alpha.md", _order("alpha"))
    found = mc.discover_standing_orders(tree)
    assert found == sorted(found)
    assert "protocols/standing-orders/user/alpha.md" in found
    assert "protocols/standing-orders/user/zeta.md" in found


# ------------------------------------------------------------ count_tokens --

def test_bytes_method_estimates_and_reports_itself():
    n, used = mc.count_tokens("x" * 240, "bytes", 2.4, "claude-opus-5")
    assert n == 100
    assert used == "bytes"


def test_api_that_cannot_run_never_reports_itself_as_exact(monkeypatch):
    """An estimate wearing the label `api` is the one outcome forbidden here."""
    monkeypatch.setattr(mc, "_api_token_count", lambda text, model: None)
    n, used = mc.count_tokens("x" * 240, "api", 2.4, "claude-opus-5")
    assert used == "bytes"
    assert n == 100


# --------------------------------------------------------------- the rows --

def test_a_file_under_its_cap_is_ok(tree):
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"] == "AGENTS.md")
    assert row["state"] == "ok"
    assert row["bytes"] == 500


def test_a_file_over_its_cap_is_over(tree):
    (tree / "AGENTS.md").write_text("A" * 5000, encoding="utf-8")
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"] == "AGENTS.md")
    assert row["state"] == "over"


def test_a_declared_item_without_a_cap_is_uncapped(tree):
    budget = mc.load_budget(tree)
    budget["items"]["AGENTS.md"].pop("max_bytes")
    rows = mc.collect_rows(tree, budget, "bytes")
    row = next(r for r in rows if r["path"] == "AGENTS.md")
    assert row["state"] == "uncapped"


def test_an_always_on_file_missing_from_the_budget_is_undeclared(tree):
    """The meter must not go blind when the tree grows."""
    _write(tree, "protocols/standing-orders/newcomer.md", _order("newcomer"))
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"].endswith("newcomer.md"))
    assert row["state"] == "undeclared"


def test_an_optional_item_that_is_absent_is_not_a_failure(tree):
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"] == "ecosystem.yaml")
    assert row["state"] == "absent"


def test_a_required_item_that_is_absent_is_missing(tree):
    (tree / "work" / "board.md").unlink()
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"] == "work/board.md")
    assert row["state"] == "missing"


def test_the_user_overlay_replaces_an_item_by_key(tree):
    _write(
        tree,
        "context-budget.user.yaml",
        "items:\n  AGENTS.md:\n    max_bytes: 100\n",
    )
    budget = mc.load_budget(tree)
    assert budget["items"]["AGENTS.md"]["max_bytes"] == 100


def test_a_command_item_measures_what_the_command_prints(tree):
    """The payload is the output, not a file, because that is what loads."""
    _write(
        tree,
        "context-budget.user.yaml",
        'items:\n  "cmd:python3 scripts/echo.py":\n    max_bytes: 1000\n',
    )
    _write(tree, "scripts/echo.py", "print('x' * 41)\n")
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"].startswith("cmd:"))
    assert row["bytes"] == 42  # 41 x plus the newline
    assert row["state"] == "ok"


def test_a_command_item_over_its_cap_is_over(tree):
    _write(
        tree,
        "context-budget.user.yaml",
        'items:\n  "cmd:python3 scripts/echo.py":\n    max_bytes: 10\n',
    )
    _write(tree, "scripts/echo.py", "print('x' * 41)\n")
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"].startswith("cmd:"))
    assert row["state"] == "over"


def test_a_command_outside_the_allowlist_is_refused(tree):
    """A budget file is reviewed, but it is still config. Config that can run
    anything is not the file anybody reviewed it as."""
    _write(
        tree,
        "context-budget.user.yaml",
        'items:\n  "cmd:rm -rf /":\n    max_bytes: 10\n',
    )
    with pytest.raises(SystemExit):
        mc.collect_rows(tree, mc.load_budget(tree), "bytes")


def test_a_command_that_fails_is_a_finding_not_a_zero(tree):
    """A command that cannot run loads nothing, and nothing measured as zero
    reads exactly like a payload that shrank to nothing."""
    _write(
        tree,
        "context-budget.user.yaml",
        'items:\n  "cmd:python3 scripts/boom.py":\n    max_bytes: 1000\n',
    )
    _write(tree, "scripts/boom.py", "import sys; sys.exit(3)\n")
    rows = mc.collect_rows(tree, mc.load_budget(tree), "bytes")
    row = next(r for r in rows if r["path"].startswith("cmd:"))
    assert row["state"] == "missing"


def test_a_missing_core_budget_is_a_hard_error(tmp_path):
    with pytest.raises(SystemExit):
        mc.load_budget(tmp_path)


# ------------------------------------------------------------- the report --

def test_the_report_is_byte_identical_across_runs(tree):
    budget = mc.load_budget(tree)
    a = mc.render_report(mc.collect_rows(tree, budget, "bytes"), "bytes", 2.4)
    b = mc.render_report(mc.collect_rows(tree, budget, "bytes"), "bytes", 2.4)
    assert a == b


def test_the_report_names_the_method_and_flags_an_estimate(tree):
    budget = mc.load_budget(tree)
    out = mc.render_report(mc.collect_rows(tree, budget, "bytes"), "bytes", 2.4)
    assert "bytes" in out
    assert "estimate" in out.lower()
    assert "2.4" in out


# --------------------------------------------------------------- the gate --

def test_a_clean_tree_exits_zero(tree):
    assert mc.main(["--repo-root", str(tree), "--method", "bytes"]) == 0


def test_an_over_cap_file_trips_the_gate(tree):
    (tree / "AGENTS.md").write_text("A" * 5000, encoding="utf-8")
    assert mc.main(["--repo-root", str(tree), "--method", "bytes"]) == 1


def test_an_undeclared_always_on_file_trips_the_gate(tree):
    _write(tree, "protocols/standing-orders/newcomer.md", _order("newcomer"))
    assert mc.main(["--repo-root", str(tree), "--method", "bytes"]) == 1


def test_a_missing_required_file_trips_the_gate(tree):
    (tree / "work" / "board.md").unlink()
    assert mc.main(["--repo-root", str(tree), "--method", "bytes"]) == 1


def test_uncapped_alone_does_not_trip_the_gate(tree):
    """Warn, never block: a new item must be addable in one line."""
    _write(
        tree,
        "context-budget.user.yaml",
        "items:\n  AGENTS.md: {}\n",
    )
    assert mc.main(["--repo-root", str(tree), "--method", "bytes"]) == 0
