#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/context-index.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS EXISTS. `context-budget.yaml` measures what a session loads and holds
it to a ceiling. It cannot make anything smaller. Two always-on sources are
large because they are whole files where only a table of contents is needed
before the work starts: a registry naming every repo, customer and workspace,
and any other declared map an instance keeps. A session needs to know THAT a
customer called `example-customer` exists and roughly what it is. It does not
need the customer's fifteen fields until somebody names it.

Skills have had this split since the beginning: the name and one line stay
resident, the body arrives on invocation. Standing orders got it when the load
contract landed. This adds it to declared map sources, and does it once rather
than a third time in a third shape.

    THE CARD (what stays resident)

        keep       top-level keys emitted VERBATIM, comments and all
        sections   top-level maps whose children become one index line each
        label      the child field that supplies that line (default:
                   `description`), truncated to `label_chars` (default 120)

    Anything else present in the file is named under "also present", never
    dropped. A card that silently omits a key is the exact failure the budget
    was built to stop, one layer down.

    THE BODY (what arrives on demand)

        --get <dotted.path>    the raw text of that block, comments included
        --list                 every path --get accepts

    Slices are RAW TEXT, never re-serialized YAML. Re-serializing drops
    comments, and in these files the comments carry the reasoning: which
    branch is the running one, which board is closed, why a path is what it
    is. A reader that strips them answers the letter of the question and loses
    the part that stops a wrong action.

    FAIL-OPEN. A file with no `card:` declaration is still indexable: every
    top-level map with map children is treated as a section. An instance that
    has never heard of this feature loses nothing by not declaring.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "context-index.py"


def _load_lib():
    """Import scripts/lib/context_index.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "context_index", REPO_ROOT / "scripts" / "lib" / "context_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _load_lib()


# --------------------------------------------------------------- fixtures --

SOURCE = """\
# yaml-language-server: $schema=./docs/schemas/ecosystem.schema.yaml
---
# A header comment about the file as a whole.

org: example-org
local_root: ~/Projects

work_system:
  enabled: true
  path: work/

base:
  # This comment belongs to my-bridge and has to travel with it.
  my-bridge:
    github: example/my-bridge
    description: "Your own Bridge instance."
    type: tool

  wiki:
    github: example/wiki
    description: "Documentation, protocols and customer records, kept in one place so that every instance can point at the same paragraph instead of its own copy."
    type: docs

customers:
  # An orphan comment, separated from the entry below by a blank line.

  example-customer:
    display_name: "Example Customer"
    issue_repo: example/example-wiki

github_projects:
  - number: 1
    name: "Board one"
  - number: 2
    name: "Board two"
"""

CARD = {
    "kind": "index",
    "keep": ["org", "local_root", "work_system"],
    "sections": ["base", "customers"],
    "label": "description",
    "label_chars": 40,
}


@pytest.fixture()
def source() -> str:
    return SOURCE


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "ecosystem.yaml").write_text(SOURCE, encoding="utf-8")
    return tmp_path


def run_cli(repo: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(CLI), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
    )


# ------------------------------------------------------- addressing + get --


def test_list_names_every_top_level_key_and_section_child(source):
    """--list is the contract of what --get accepts, and it covers the file."""
    paths = ci.addressable(source, CARD)

    for top in ("org", "local_root", "work_system", "base", "customers",
                "github_projects"):
        assert top in paths, f"top-level key {top} is not addressable"
    assert "base.my-bridge" in paths
    assert "base.wiki" in paths
    assert "customers.example-customer" in paths


def test_get_a_top_level_scalar_returns_its_line(source):
    assert ci.slice_block(source, "org").strip() == "org: example-org"


def test_get_a_section_returns_the_whole_block_verbatim(source):
    block = ci.slice_block(source, "base")

    assert block.startswith("base:")
    assert "my-bridge:" in block and "wiki:" in block
    # Verbatim means the comment inside the block survives.
    assert "This comment belongs to my-bridge" in block
    # And it stops at the next top-level key.
    assert "customers:" not in block


def test_get_an_entry_returns_only_that_entry(source):
    block = ci.slice_block(source, "base.my-bridge")

    assert "my-bridge:" in block
    assert "github: example/my-bridge" in block
    assert "wiki:" not in block


def test_get_carries_the_comment_directly_above_the_entry(source):
    """A comment touching an entry is that entry's, and travels with it.

    This is the whole reason slices are text and not re-serialized YAML. In a
    real registry these lines say things like which branch is actually
    running, and losing them is losing the guardrail, not the prose.
    """
    block = ci.slice_block(source, "base.my-bridge")
    assert "This comment belongs to my-bridge" in block


def test_get_leaves_a_comment_separated_by_a_blank_line(source):
    """Contiguity is the rule; a blank line ends the attachment."""
    block = ci.slice_block(source, "customers.example-customer")

    assert "example-customer:" in block
    assert "An orphan comment" not in block


def test_get_an_unknown_path_says_what_exists(repo):
    result = run_cli(repo, "ecosystem.yaml", "--get", "customers.nope")

    assert result.returncode != 0
    # Naming the neighbours is the difference between a usable error and a
    # traceback: the caller guessed a name and needs the real ones.
    assert "example-customer" in (result.stdout + result.stderr)


def test_every_listed_path_is_retrievable(source):
    """THE ROUND TRIP. An index that points at nothing is worse than none.

    Worse, because a pointer reads as a promise that the content is one call
    away, and the caller stops looking for it anywhere else.
    """
    for path in ci.addressable(source, CARD):
        block = ci.slice_block(source, path)
        assert block.strip(), f"{path} is listed but slices to nothing"


# ---------------------------------------------------------------- the card --


def test_card_names_every_top_level_key(source):
    """NOTHING VANISHES. Kept, sectioned or merely named, but never absent."""
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    for top in ("org", "local_root", "work_system", "base", "customers",
                "github_projects"):
        assert top in card, f"{top} is in the file and not in the card"


def test_kept_keys_are_emitted_as_raw_text(source):
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    assert "org: example-org" in card
    assert "local_root: ~/Projects" in card
    assert "enabled: true" in card


def test_section_entries_appear_with_their_label(source):
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    assert "my-bridge" in card
    assert "Your own Bridge instance." in card


def test_a_long_label_is_truncated_visibly(source):
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    assert "…" in card, "truncation has to be visible, or it reads as the whole label"
    assert "instead of its own copy." not in card


def test_an_entry_without_a_label_still_appears(source):
    """Omitting the unlabelled ones would hide exactly what nobody described."""
    card = ci.render_card(source, CARD, "ecosystem.yaml")
    assert "example-customer" in card


def test_label_falls_back_through_a_list_of_fields():
    """Not every entry family names its one line the same thing.

    Measured against a real 22 KB registry: every customer carries
    `display_name` and no `description`, so a single-field label left a
    quarter of the index reading "(no label)" — an index whose lines are
    blank is a list of names, which is the thing this was supposed to beat.
    """
    text = (
        "customers:\n"
        "  one:\n"
        "    display_name: \"Customer One\"\n"
        "  two:\n"
        "    description: \"The second.\"\n"
    )
    card = ci.render_card(
        text, {"sections": ["customers"], "label": ["description", "display_name"]},
        "e.yaml",
    )

    assert "Customer One" in card
    assert "The second." in card


def test_card_names_the_command_that_fetches_a_body(source):
    """A card that does not say how to get the rest is a card with a dead end."""
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    assert "context-index.py" in card
    assert "--get" in card


def test_card_is_deterministic(source):
    """No timestamp, no host, no dict ordering: it is compared across runs."""
    assert ci.render_card(source, CARD, "ecosystem.yaml") == ci.render_card(
        source, CARD, "ecosystem.yaml"
    )


def test_a_list_valued_key_is_reported_with_its_length(source):
    """Lists are not indexed in this contract, and say so rather than vanish."""
    card = ci.render_card(source, CARD, "ecosystem.yaml")

    assert "github_projects" in card
    assert "2" in card


def test_an_undeclared_file_still_indexes(source):
    """FAIL-OPEN. No card declaration, sections auto-detected, nothing lost."""
    card = ci.render_card(source, None, "ecosystem.yaml")

    assert "my-bridge" in card
    assert "example-customer" in card
    for top in ("org", "local_root", "github_projects"):
        assert top in card


def test_auto_detection_actually_indexes_a_section(source):
    """The name appearing is not proof the section was detected.

    Written after the previous test passed for the wrong reason: entry names
    also appear when a block is kept WHOLE, so an assertion on the name alone
    is green whether detection worked or not. The distinguishing evidence is
    that the entry's fields are gone.
    """
    card = ci.render_card(source, None, "ecosystem.yaml")

    assert "## base" in card, "base is a map of maps and has to be a section"
    assert "github: example/my-bridge" not in card, (
        "an indexed section must not still carry its entries' fields"
    )


def test_a_block_does_not_swallow_the_next_keys_comment():
    """A comment touching the key BELOW it introduces that key, not the one above.

    Found by rendering the registry this repo actually ships: `local_root`
    came back carrying the paragraph that introduces `work_system`. Harmless
    to read, wrong to slice, and wrong in the direction that matters — the
    next key's reasoning shows up filed under its neighbour.
    """
    text = (
        "local_root: ~/Projects\n"
        "\n"
        "# This paragraph introduces work_system.\n"
        "work_system:\n"
        "  enabled: true\n"
    )

    assert "introduces work_system" not in ci.slice_block(text, "local_root")
    assert "introduces work_system" in ci.slice_block(text, "work_system")


def test_auto_detection_keeps_a_mixed_map_whole():
    """A map of settings is configuration, not a section of entries.

    Also found against the shipped registry: `work_system` was detected as a
    section because one of its three children happens to be a map, so
    `enabled: true` stopped being resident. That value decides whether Phase 1
    runs at all. When children are mixed, auto-detection keeps the block —
    the fail-open direction is to carry more, never less.
    """
    text = (
        "work_system:\n"
        "  enabled: true\n"
        "  path: work/\n"
        "  files:\n"
        "    current: work/log.md\n"
        "\n"
        "customers:\n"
        "  one:\n"
        "    description: First.\n"
    )
    card = ci.render_card(text, None, "e.yaml")

    assert "enabled: true" in card, "a settings block has to stay resident"
    assert "## work_system" not in card
    assert "## customers" in card


# ---------------------------------------------- the loss, made measurable --


def test_card_reports_comment_bytes_it_cannot_carry(source):
    """A guardrail written as a YAML comment stops working the day the file
    stops being read whole. That is a real cost of this feature, so the card
    states it rather than letting a migration discover it later."""
    lost = ci.uncarried_comment_bytes(source, CARD)
    assert lost > 0

    card = ci.render_card(source, CARD, "ecosystem.yaml")
    assert str(lost) in card


def test_comment_bytes_ignore_kept_regions(source):
    """A comment inside a kept key travels, so it is not a loss."""
    card_keeping_base = dict(CARD, keep=["org", "local_root", "work_system", "base"],
                             sections=["customers"])
    assert ci.uncarried_comment_bytes(
        source, card_keeping_base
    ) < ci.uncarried_comment_bytes(source, CARD)


# ------------------------------------------------------------------- CLI --


def test_cli_prints_the_card_by_default(repo):
    result = run_cli(repo, "ecosystem.yaml")

    assert result.returncode == 0
    assert "my-bridge" in result.stdout


def test_cli_get_prints_one_block(repo):
    result = run_cli(repo, "ecosystem.yaml", "--get", "base.my-bridge")

    assert result.returncode == 0
    assert "github: example/my-bridge" in result.stdout
    assert "wiki:" not in result.stdout


def test_cli_list_prints_addressable_paths(repo):
    result = run_cli(repo, "ecosystem.yaml", "--list")

    assert result.returncode == 0
    assert "base.my-bridge" in result.stdout


def test_cli_on_a_missing_file_is_quiet_and_succeeds(repo):
    """A registry is instance data. Absent is the normal state of a fresh
    clone, and a session that fails there fails for everyone who has not
    onboarded yet."""
    result = run_cli(repo, "does-not-exist.yaml")

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_cli_reads_the_card_from_the_budget(tmp_path):
    """The declaration lives in one place, and both halves read the same one."""
    (tmp_path / "ecosystem.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "context-budget.yaml").write_text(
        "schema_version: 1\n"
        "items:\n"
        "  ecosystem.yaml:\n"
        "    card:\n"
        "      kind: index\n"
        "      keep: [org]\n"
        "      sections: [customers]\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "ecosystem.yaml")

    assert result.returncode == 0
    assert "org: example-org" in result.stdout
    # `base` is neither kept nor sectioned here, so it is named, not expanded.
    assert "my-bridge" not in result.stdout
    assert "base" in result.stdout


# ------------------------------------------------- the guards, and biting --


def test_round_trip_guard_reports_an_unreachable_name():
    """MUTATION. The guard has to fail on a card that promises a dead path."""
    findings = ci.check_round_trip(SOURCE, CARD, extra_paths=["customers.ghost"])
    assert any("ghost" in f for f in findings)


def test_round_trip_guard_is_silent_on_a_healthy_file():
    assert ci.check_round_trip(SOURCE, CARD) == []


def test_nothing_vanishes_guard_reports_an_omitted_key():
    """MUTATION. A card that drops a top-level key has to be a finding."""
    findings = ci.check_coverage(SOURCE, rendered="org: example-org\n")
    assert any("customers" in f for f in findings)


def test_nothing_vanishes_guard_is_silent_on_the_real_card():
    card = ci.render_card(SOURCE, CARD, "ecosystem.yaml")
    assert ci.check_coverage(SOURCE, rendered=card) == []


# ---------------------------------------------------------- the live tree --


def test_the_shipped_example_registry_round_trips():
    """Run against what this repo actually ships, not only a fixture.

    A contract proven on a hand-written fixture and never on real content is
    the shape that passes CI and fails on the first instance.
    """
    example = REPO_ROOT / "ecosystem.example.yaml"
    if not example.is_file():
        pytest.skip("no ecosystem.example.yaml in this tree")

    text = example.read_text(encoding="utf-8")
    assert ci.check_round_trip(text, None) == []
    card = ci.render_card(text, None, "ecosystem.example.yaml")
    assert ci.check_coverage(text, rendered=card) == []
    assert len(card.encode("utf-8")) < len(text.encode("utf-8")), (
        "a card that is not smaller than its source buys nothing"
    )


# ------------------------------------------- the declaration's own guard --
#
# The declaration is the new failure surface this feature adds. A name in
# `keep:` or `sections:` that does not exist in the source is a typo the card
# absorbs in silence: the real key falls through to "also present" and its
# content stops being resident, and the card still renders and still passes.


def test_a_declared_section_that_does_not_exist_is_a_finding(source):
    findings = ci.check_declaration(source, dict(CARD, sections=["custommers"]))
    assert any("custommers" in f for f in findings)


def test_a_declared_keep_that_does_not_exist_is_a_finding(source):
    findings = ci.check_declaration(source, dict(CARD, keep=["org", "loca_root"]))
    assert any("loca_root" in f for f in findings)


def test_a_correct_declaration_is_silent(source):
    assert ci.check_declaration(source, CARD) == []


def test_an_absent_declaration_is_silent(source):
    """Auto-detection reads the file, so it cannot name something that is not
    in it. Nothing to check, and nothing to complain about."""
    assert ci.check_declaration(source, None) == []


def test_check_runs_every_declared_card_in_the_tree(tmp_path):
    (tmp_path / "ecosystem.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "context-budget.yaml").write_text(
        "schema_version: 1\n"
        "items:\n"
        "  ecosystem.yaml:\n"
        "    card:\n"
        "      kind: index\n"
        "      sections: [custommers]\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "--check")

    assert result.returncode != 0
    assert "custommers" in (result.stdout + result.stderr)


def test_check_is_green_on_a_healthy_tree(tmp_path):
    (tmp_path / "ecosystem.yaml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "context-budget.yaml").write_text(
        "schema_version: 1\n"
        "items:\n"
        "  ecosystem.yaml:\n"
        "    card:\n"
        "      kind: index\n"
        "      keep: [org]\n"
        "      sections: [base, customers]\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "--check")

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_on_a_tree_with_no_cards_is_green(tmp_path):
    (tmp_path / "context-budget.yaml").write_text(
        "schema_version: 1\nitems: {}\n", encoding="utf-8"
    )
    assert run_cli(tmp_path, "--check").returncode == 0
