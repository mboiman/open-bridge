#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for the rule inventory that scripts/validate-bridge.py emits.

CONTRACT, this file is the authoritative spec for that surface.

    collect_rule_rows(repo_root: Path) -> list[dict]
        One row per `rules/**/*.md`, sorted by path so the map never churns.
        Each row carries:
            path    repo-relative, POSIX separators
            tier    the declared `scope:` frontmatter; falls back to the tier
                    folder (`rules/org/**` -> org, `rules/user/**` -> user,
                    top level -> core); "?" when neither is available
            state   whether a NEW rule may be authored at that path, see below

    rule_state(rel, ignored, tracked) -> str
        The load-bearing column, and the reason this map exists at all:

            authorable  git will track a new file here
            managed     the path is gitignored and nothing is tracked there.
                        A new file here is invisible to git AND skipped by
                        scripts/overlay-export.py, which asks `git check-ignore`
                        and does not care about tracked state. Authoring there
                        loses the file silently.
            legacy      gitignored, yet tracked. An existing exception, not a
                        place to add to.
            unknown     git could not be consulted.

        `unknown` must NEVER be reported as `authorable`. An unknown ignore
        state that reads as "go ahead" is exactly how a rule gets written into
        a folder it can never leave.

    render_rule_map(rows) -> str
        Deterministic markdown. No timestamp, no host, nothing that changes
        between two runs over an unchanged tree: the file is regenerated on
        every validate run and a churning map is noise in every diff.
        Table cells are escaped, so a path may contain `|`.

    write_rule_map(repo_root) -> Path
        Writes `.bridge/rule-scope.md`. Derived, per-instance, gitignored, the
        same treatment `.bridge/skill-scope.md` already gets, and for the same
        reason: a table generated from the LOCAL tree cannot converge across
        instances, so it must not live in a CORE file.

    main() surface
        (no flag)   validate, then refresh the map
        --check     validate, write NOTHING
        The map is written even when validation fails. The inventory answers
        "what exists and where may I write", which is independent of whether
        every file currently passes.

NOT covered on purpose: the exact prose of the map. It is a view; the tests
assert the data that reaches it.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALIDATE_BRIDGE = REPO_ROOT / "scripts" / "validate-bridge.py"


def _load(path: Path, name: str) -> types.ModuleType:
    """Import a hyphenated script by path without leaving a __pycache__ entry."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


vb = _load(VALIDATE_BRIDGE, "validate_bridge_under_test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_rule(root: Path, rel: str, scope: str | None = "core") -> Path:
    """Create <root>/<rel> with an optional `scope:` frontmatter block."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    head = f"---\nscope: {scope}\n---\n" if scope is not None else "---\nsummary: x\n---\n"
    p.write_text(head + "# Rule\n", encoding="utf-8")
    return p


def git_repo(root: Path, *, gitignore: str = "") -> Path:
    """A real git checkout — git is the authority this feature consults."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    if gitignore:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    return root


def track(root: Path, *rels: str) -> None:
    subprocess.run(["git", "add", "-f", *rels], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root, check=True)


# ---------------------------------------------------------------------------
# The inventory itself
# ---------------------------------------------------------------------------


def test_every_rule_file_appears_once_with_its_declared_tier(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/theme.md", "core")
    write_rule(tmp_path, "rules/org/wiki.md", "org")
    write_rule(tmp_path, "rules/user/local.md", "user")
    rows = vb.collect_rule_rows(tmp_path)
    assert [r["path"] for r in rows] == [
        "rules/org/wiki.md", "rules/theme.md", "rules/user/local.md",
    ]
    assert {r["path"]: r["tier"] for r in rows} == {
        "rules/theme.md": "core",
        "rules/org/wiki.md": "org",
        "rules/user/local.md": "user",
    }


def test_rows_are_sorted_so_the_map_does_not_churn(tmp_path):
    git_repo(tmp_path)
    for rel in ("rules/zeta.md", "rules/alpha.md", "rules/user/mid.md"):
        write_rule(tmp_path, rel)
    rows = vb.collect_rule_rows(tmp_path)
    assert [r["path"] for r in rows] == sorted(r["path"] for r in rows)


def test_tier_falls_back_to_the_folder_when_frontmatter_is_missing(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/org/no-scope.md", scope=None)
    write_rule(tmp_path, "rules/user/no-scope.md", scope=None)
    write_rule(tmp_path, "rules/top.md", scope=None)
    tiers = {r["path"]: r["tier"] for r in vb.collect_rule_rows(tmp_path)}
    assert tiers["rules/org/no-scope.md"] == "org"
    assert tiers["rules/user/no-scope.md"] == "user"
    assert tiers["rules/top.md"] == "core"


def test_non_markdown_and_nested_non_rule_files_are_ignored(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/real.md")
    (tmp_path / "rules" / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "other.md").write_text("---\nscope: core\n---\n", encoding="utf-8")
    assert [r["path"] for r in vb.collect_rule_rows(tmp_path)] == ["rules/real.md"]


def test_a_tree_without_rules_yields_no_rows_and_does_not_raise(tmp_path):
    git_repo(tmp_path)
    assert vb.collect_rule_rows(tmp_path) == []


# ---------------------------------------------------------------------------
# The state column — the reason the map exists
# ---------------------------------------------------------------------------


def test_a_normal_folder_is_authorable(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/user/local.md")
    rows = vb.collect_rule_rows(tmp_path)
    assert rows[0]["state"] == "authorable"


def test_an_ignored_folder_with_nothing_tracked_is_managed(tmp_path):
    git_repo(tmp_path, gitignore="rules/org/**\n")
    write_rule(tmp_path, "rules/org/from-overlay.md", "org")
    write_rule(tmp_path, "rules/user/mine.md", "user")
    states = {r["path"]: r["state"] for r in vb.collect_rule_rows(tmp_path)}
    assert states["rules/org/from-overlay.md"] == "managed"
    assert states["rules/user/mine.md"] == "authorable"


def test_gitignored_but_tracked_is_legacy_not_managed(tmp_path):
    """A tracked file under an ignored path is an existing exception.

    Calling it `managed` would claim the file does not travel, which is false
    for something git already carries.
    """
    git_repo(tmp_path, gitignore="rules/org/**\n")
    write_rule(tmp_path, "rules/org/historic.md", "org")
    write_rule(tmp_path, "rules/org/fresh.md", "org")
    track(tmp_path, "rules/org/historic.md")
    states = {r["path"]: r["state"] for r in vb.collect_rule_rows(tmp_path)}
    assert states["rules/org/historic.md"] == "legacy"
    assert states["rules/org/fresh.md"] == "managed"


def test_outside_a_git_checkout_the_state_is_unknown_never_authorable(tmp_path):
    write_rule(tmp_path, "rules/user/local.md")
    rows = vb.collect_rule_rows(tmp_path)
    assert rows[0]["state"] == "unknown"


def test_a_failing_check_ignore_degrades_to_unknown_never_authorable(tmp_path, monkeypatch):
    """Unknown ignore state must not read as "go ahead"."""
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/user/local.md")
    real = subprocess.run

    def broken(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "check-ignore" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "boom")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(vb.subprocess, "run", broken)
    rows = vb.collect_rule_rows(tmp_path)
    assert rows[0]["state"] == "unknown"


def test_rule_state_never_invents_authorable_from_none():
    """The pure function, exercised directly on an unknown ignore set."""
    assert vb.rule_state("rules/x.md", ignored=None, tracked=None) == "unknown"
    assert vb.rule_state("rules/x.md", ignored=None, tracked={"rules/x.md"}) == "unknown"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_is_byte_identical_across_two_runs(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    rows = vb.collect_rule_rows(tmp_path)
    assert vb.render_rule_map(rows) == vb.render_rule_map(rows)


def test_render_carries_no_timestamp_or_host(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    text = vb.render_rule_map(vb.collect_rule_rows(tmp_path))
    assert "20" not in text.split("|")[0]  # no leading date line
    for noisy in ("generated at", "Generated at", "hostname"):
        assert noisy not in text


def test_a_pipe_in_a_path_does_not_break_the_table(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/we|ird.md")
    rows = vb.collect_rule_rows(tmp_path)
    line = [ln for ln in vb.render_rule_map(rows).splitlines() if "ird.md" in ln][0]
    assert line.count("|") - line.count("\\|") == 4  # 3 cells => 4 delimiters


def test_render_names_every_row(tmp_path):
    git_repo(tmp_path, gitignore="rules/org/**\n")
    write_rule(tmp_path, "rules/a.md")
    write_rule(tmp_path, "rules/org/b.md", "org")
    write_rule(tmp_path, "rules/user/c.md", "user")
    text = vb.render_rule_map(vb.collect_rule_rows(tmp_path))
    for rel in ("rules/a.md", "rules/org/b.md", "rules/user/c.md"):
        assert rel in text


# ---------------------------------------------------------------------------
# Writing + the main() surface
# ---------------------------------------------------------------------------


def test_write_rule_map_lands_in_dot_bridge(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    out = vb.write_rule_map(tmp_path)
    assert out == tmp_path / ".bridge" / "rule-scope.md"
    assert out.exists() and "rules/a.md" in out.read_text(encoding="utf-8")


def test_write_creates_the_bridge_directory_when_absent(tmp_path):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    assert not (tmp_path / ".bridge").exists()
    vb.write_rule_map(tmp_path)
    assert (tmp_path / ".bridge").is_dir()


def test_main_refreshes_the_map(tmp_path, monkeypatch):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    monkeypatch.setattr(vb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["validate-bridge.py"])
    vb.main()
    assert (tmp_path / ".bridge" / "rule-scope.md").exists()


def test_main_check_writes_nothing(tmp_path, monkeypatch):
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/a.md")
    monkeypatch.setattr(vb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["validate-bridge.py", "--check"])
    vb.main()
    assert not (tmp_path / ".bridge" / "rule-scope.md").exists()


def test_the_map_is_written_even_when_a_rule_fails_validation(tmp_path, monkeypatch):
    """The inventory answers "what exists", not "what passes"."""
    git_repo(tmp_path)
    write_rule(tmp_path, "rules/broken.md", scope=None)  # no scope: -> fails
    monkeypatch.setattr(vb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["validate-bridge.py"])
    rc = vb.main()
    assert rc != 0
    assert (tmp_path / ".bridge" / "rule-scope.md").exists()
