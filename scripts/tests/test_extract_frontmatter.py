#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/extract-frontmatter.py.

CONTRACT.

    extract(path: Path) -> dict | None
        Finds the frontmatter block and hands its RAW text to PyYAML, so the
        values are whatever a real YAML parser makes of them. This script's
        only own job is deciding where the block starts and ends.

        Block detection:
          * `# ...` comment lines and blank lines before the block are skipped
            (the `# yaml-language-server: $schema=...` prolog convention).
          * The opening and closing `---` fences count at COLUMN 0 only. A
            block scalar's continuation lines are indented by definition, so
            an indented `---` inside a `title: |` block is content. Accepting
            it as a fence ended the block early and dropped every key after
            it, silently, with exit 0.
          * The first non-blank, non-comment, non-fence line means there is no
            frontmatter: returns None.

        Exits, all on stderr:
          1  the block opens and never closes
          2  the file does not exist, or PyYAML rejects the block

        Returns None for a block that is present but empty.

Unlike scripts/okf-export.py's parse_frontmatter, this script REQUIRES PyYAML
and is allowed to: it is a developer utility, not the dependency-free
exporter. The two therefore agree on where a block is and deliberately differ
on tolerance, since PyYAML raises where the exporter keeps a malformed value.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "extract-frontmatter.py"


def _load(path: Path, name: str) -> types.ModuleType:
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


ef = _load(SCRIPT, "extract_frontmatter_under_test")


def md(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The defect.
# ---------------------------------------------------------------------------


def test_indented_fence_inside_a_block_scalar_does_not_close_the_block(tmp_path):
    """Silent, and it dropped a whole key.

    `---` inside a `title: |` block used to end the frontmatter. The result
    was exit 0 and a dict missing everything below, so a caller could not tell
    a truncated read from a short file.
    """
    path = md(
        tmp_path,
        "---\ntitle: |\n  Heading\n  ---\n  Trailer\nstatus: doing\n---\n\nbody\n",
    )
    result = ef.extract(path)
    assert result is not None
    assert result["status"] == "doing", "everything after the indented --- was dropped"
    assert result["title"] == "Heading\n---\nTrailer\n"


def test_it_agrees_with_pyyaml_on_the_whole_block(tmp_path):
    """The block this script hands over must be the block PyYAML would read."""
    block = "title: |\n  Heading\n  ---\n  Trailer\nstatus: doing\ncount: 3"
    path = md(tmp_path, f"---\n{block}\n---\n\nbody\n")
    assert ef.extract(path) == yaml.safe_load(block)


# ---------------------------------------------------------------------------
# The rest of the contract, previously untested.
# ---------------------------------------------------------------------------


def test_a_plain_block_is_parsed_by_pyyaml(tmp_path):
    path = md(tmp_path, '---\ntitle: "a # hash"\nnums: [1, 2]\n---\nbody\n')
    assert ef.extract(path) == {"title": "a # hash", "nums": [1, 2]}


def test_the_yaml_language_server_prolog_is_skipped(tmp_path):
    path = md(
        tmp_path,
        "# yaml-language-server: $schema=./_schema.yaml\n---\ntitle: t\n---\nbody\n",
    )
    assert ef.extract(path) == {"title": "t"}


def test_a_file_without_frontmatter_returns_none(tmp_path):
    assert ef.extract(md(tmp_path, "# A heading\n\nbody\n")) is None


def test_an_indented_opening_fence_is_not_frontmatter(tmp_path):
    """An indented `---` opens a markdown indented code block, not frontmatter.

    Accepting it made the OPENING fence and the CLOSING fence disagree about
    what a fence is, which is how a block scalar's `---` got treated as one.
    The same rule on both ends is what makes the pair coherent.
    """
    path = md(tmp_path, "  ---\n  title: t\n  ---\n\nbody\n")
    assert ef.extract(path) is None


def test_an_empty_block_returns_none(tmp_path):
    assert ef.extract(md(tmp_path, "---\n---\nbody\n")) is None


def test_an_unclosed_block_exits_1(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        ef.extract(md(tmp_path, "---\ntitle: t\n\nbody with no closing fence\n"))
    assert excinfo.value.code == 1


def test_invalid_yaml_exits_2(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        ef.extract(md(tmp_path, "---\ntitle: 'unterminated\n---\nbody\n"))
    assert excinfo.value.code == 2


def test_a_missing_file_exits_2(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        ef.extract(tmp_path / "nope.md")
    assert excinfo.value.code == 2


def test_a_horizontal_rule_in_the_body_is_not_a_second_block(tmp_path):
    path = md(tmp_path, "---\ntitle: t\n---\n\nintro\n\n---\n\nmore body\n")
    assert ef.extract(path) == {"title": "t"}


def test_every_frontmatter_bearing_file_in_this_repo_still_reads(tmp_path):
    """Regression over the repo's own corpus: tightening the fence rule must
    not turn a file that used to parse into one that does not."""
    corpus = [
        p
        for p in REPO_ROOT.rglob("*.md")
        if ".git/" not in str(p) and p.read_text(encoding="utf-8").startswith(("---", "# yaml-"))
    ]
    if not corpus:
        pytest.skip("no frontmatter-bearing markdown in this repo")
    for path in corpus:
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        fences = [i for i, line in enumerate(lines) if line == "---"]
        if len(fences) < 2:
            continue
        try:
            expected = yaml.safe_load("\n".join(lines[fences[0] + 1 : fences[1]]))
        except yaml.YAMLError:
            continue  # the script is allowed to exit 2 on these
        if not expected:
            continue
        assert ef.extract(path) == expected, f"{path} no longer reads as PyYAML reads it"
