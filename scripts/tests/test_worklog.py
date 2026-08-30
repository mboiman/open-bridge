#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/worklog.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS EXISTS. Phase 1 says to read `work/log.md` for "last activity, current
week". Nothing enforced the second half, and reading a file reads the file: on a
live instance the log measured 405,076 bytes, roughly 155,800 tokens, four times
everything else in the always-on budget put together. The instruction was prose,
and prose does not truncate a file.

This is the same shape as the config slice: name what the session needs, and emit
exactly that.

    parse_blocks(text) -> (header, blocks)
        `header` is everything before the first `## ` heading: the week line and
        the rolling TODO, which are session context in their own right. `blocks`
        is one entry per day heading.

    recent_blocks(blocks, count) -> list
        The `count` most recent, by the `DD.MM` in the heading. Order in the file
        is NOT assumed: an instance that appends day blocks at the end must get
        the same answer as one that prepends them. An unparseable heading falls
        back to file order rather than being dropped, because losing a day is
        worse than including one too many.

    render(header, blocks) -> str
        Deterministic.

    main(argv) -> int
        `--recent N` (default 3). A missing log is not an error: Phase 1 creates
        it from a template and continues.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "worklog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("worklog", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worklog"] = mod
    spec.loader.exec_module(mod)
    return mod


wl = _load_module()

HEADER = "# KW 35 (24.08 - 30.08.2026)\n\n**Focus:** something\n\n- [ ] a rolling todo\n\n"
D28 = "## Friday 28.08\n\n| 2026-08-28 19:43 | ok | ctx | twenty-eighth |\n\n"
D27 = "## Thursday 27.08\n\n| 2026-08-27 10:00 | ok | ctx | twenty-seventh |\n\n"
D26 = "## Wednesday 26.08\n\n| 2026-08-26 10:00 | ok | ctx | twenty-sixth |\n\n"
D24 = "## Monday 24.08\n\n| 2026-08-24 10:00 | ok | ctx | twenty-fourth |\n"

NEWEST_FIRST = HEADER + D28 + D27 + D26 + D24
OLDEST_FIRST = HEADER + D24 + D26 + D27 + D28


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "log.md").write_text(NEWEST_FIRST, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------- the parsing --

def test_the_header_is_everything_before_the_first_day():
    header, _ = wl.parse_blocks(NEWEST_FIRST)
    assert "KW 35" in header and "rolling todo" in header
    assert "28.08" not in header


def test_every_day_block_is_found():
    _, blocks = wl.parse_blocks(NEWEST_FIRST)
    assert len(blocks) == 4


def test_a_log_with_no_day_blocks_yields_only_a_header():
    header, blocks = wl.parse_blocks("# KW 1\n\nnothing yet\n")
    assert "KW 1" in header
    assert blocks == []


# ------------------------------------------------------------ the selection --

def test_the_most_recent_days_are_selected():
    _, blocks = wl.parse_blocks(NEWEST_FIRST)
    got = wl.recent_blocks(blocks, 2)
    assert "twenty-eighth" in "".join(got)
    assert "twenty-seventh" in "".join(got)
    assert "twenty-fourth" not in "".join(got)


def test_file_order_is_not_assumed():
    """An instance that appends day blocks must get the same answer as one that
    prepends them. Assuming an order is how a silently wrong week happens."""
    _, newest_first = wl.parse_blocks(NEWEST_FIRST)
    _, oldest_first = wl.parse_blocks(OLDEST_FIRST)
    a = set(wl.recent_blocks(newest_first, 2))
    b = set(wl.recent_blocks(oldest_first, 2))
    assert a == b


def test_asking_for_more_days_than_exist_returns_all():
    _, blocks = wl.parse_blocks(NEWEST_FIRST)
    assert len(wl.recent_blocks(blocks, 99)) == 4


def test_an_unparseable_heading_is_kept_rather_than_dropped():
    """Losing a day is worse than including one too many."""
    text = HEADER + "## Someday\n\nrow\n\n" + D28
    _, blocks = wl.parse_blocks(text)
    assert len(wl.recent_blocks(blocks, 99)) == 2


# -------------------------------------------------------------- the render --

def test_the_render_is_byte_identical_across_runs(tree):
    a = wl.main(["--repo-root", str(tree), "--recent", "2", "--to-string"])
    b = wl.main(["--repo-root", str(tree), "--recent", "2", "--to-string"])
    assert a == b


def test_the_render_is_smaller_than_the_file(tree):
    whole = (tree / "work" / "log.md").read_text(encoding="utf-8")
    out = wl.main(["--repo-root", str(tree), "--recent", "1", "--to-string"])
    assert len(out) < len(whole)


def test_the_render_keeps_the_header_because_it_is_session_context(tree):
    out = wl.main(["--repo-root", str(tree), "--recent", "1", "--to-string"])
    assert "rolling todo" in out


# ---------------------------------------------------------------- the main --

def test_main_exits_zero_and_prints(tree, capsys):
    assert wl.main(["--repo-root", str(tree), "--recent", "2"]) == 0
    assert "twenty-eighth" in capsys.readouterr().out


def test_a_missing_log_is_not_an_error(tmp_path, capsys):
    """Phase 1 creates it from a template and continues; it must not die here."""
    assert wl.main(["--repo-root", str(tmp_path), "--recent", "2"]) == 0
    assert capsys.readouterr().out.strip() == ""


# --------------------------------------------- the kept-heading contract --
#
# The module docstring: "A heading whose date will not parse is kept rather
# than dropped, because losing a day is worse than including one too many."
# `_sort_key` returned (0, 0, -index) for it against (1, ...) for a dated one,
# with reverse=True and then `ordered[:count]` — so it sorted LAST and was the
# FIRST thing cut. The contract was exactly inverted.
#
# The guarding test could not see it: it called recent_blocks(blocks, 99) on a
# two-block fixture and asserted len == 2, which is true of any implementation
# that does not delete blocks, and never exercised the production value 3.


def _blocks(*headings):
    return [f"## {h}\n| a | b |\n" for h in headings]


def test_an_unparseable_heading_survives_the_production_slice():
    blocks = _blocks("WICHTIG offene Punkte", "Samstag 30.08", "Freitag 29.08", "Donnerstag 28.08")
    kept = wl.recent_blocks(blocks, 3)
    assert any("WICHTIG offene Punkte" in b for b in kept)


def test_it_does_not_consume_one_of_the_dated_slots():
    # "including one too many" — the pinned section is extra, not a substitute
    # for a day. Otherwise adding one silently shortens the working memory.
    blocks = _blocks("WICHTIG offene Punkte", "Samstag 30.08", "Freitag 29.08", "Donnerstag 28.08")
    kept = wl.recent_blocks(blocks, 3)
    dated = [b for b in kept if "WICHTIG" not in b]
    assert len(dated) == 3


def test_several_unparseable_headings_are_all_kept():
    blocks = _blocks("Pinned A", "Pinned B", "Samstag 30.08", "Freitag 29.08")
    kept = wl.recent_blocks(blocks, 1)
    assert sum("Pinned" in b for b in kept) == 2


def test_unparseable_headings_keep_file_order():
    blocks = _blocks("Pinned A", "Pinned B", "Samstag 30.08")
    kept = wl.recent_blocks(blocks, 1)
    assert kept.index(blocks[0]) < kept.index(blocks[1])


def test_dated_blocks_are_still_newest_first(tmp_path):
    blocks = _blocks("Freitag 29.08", "Samstag 30.08", "Donnerstag 28.08")
    kept = wl.recent_blocks(blocks, 2)
    assert kept[0].startswith("## Samstag 30.08")
    assert kept[1].startswith("## Freitag 29.08")


def test_a_log_of_only_dated_blocks_is_unchanged(tmp_path):
    blocks = _blocks("Samstag 30.08", "Freitag 29.08", "Donnerstag 28.08")
    assert len(wl.recent_blocks(blocks, 2)) == 2
