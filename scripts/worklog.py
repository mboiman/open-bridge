#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Emit the part of work/log.md a session start actually needs.

Phase 1 says to read `work/log.md` for "last activity, current week". Nothing
enforced the second half, and reading a file reads the file: on a live instance
the log measured 405,076 bytes, roughly 155,800 tokens, four times everything
else in the always-on budget put together. The instruction was prose, and prose
does not truncate a file.

    python3 scripts/worklog.py --recent 3

Same shape as the config slice: name what the session needs, and emit exactly
that. The rest of the log stays where it is, and `/briefing` and `/archive` read
it in full when that is what they are for.

WHAT IS KEPT. Everything before the first day heading (the week line and the
rolling TODO, which are session context in their own right) plus the N most
recent day blocks, chosen by the date in the heading rather than by position in
the file. An instance that appends day blocks at the end must get the same answer
as one that prepends them; assuming an order is how a silently wrong week
happens. A heading whose date will not parse is kept rather than dropped, because
losing a day is worse than including one too many.

Year boundaries: headings carry `DD.MM` without a year by design (the display
anchor `/archive` and `/briefing` key off). A log spanning 31.12 and 02.01 would
therefore order those two wrongly. It is left that way on purpose: the archive
resets the log weekly, so the case is rare, and the failure is one extra old
block rather than a missing recent one.

The contract lives in `scripts/tests/test_worklog.py`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LOG_PATH = Path("work") / "log.md"
DAY_HEADING = re.compile(r"^## .*?(\d{2})\.(\d{2})\s*$")
DEFAULT_RECENT = 3


def parse_blocks(text: str) -> tuple[str, list[str]]:
    """(everything before the first `## ` heading, one string per day block)."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        return text, []
    header = "\n".join(lines[: starts[0]])
    bounds = starts + [len(lines)]
    # Trailing blank lines are formatting noise, and whether a block has them
    # depends on whether it is the last one in the file. Normalising here is
    # what makes the parse position-independent, which is the property the whole
    # date-based selection rests on.
    blocks = [
        "\n".join(lines[bounds[i] : bounds[i + 1]]).rstrip()
        for i in range(len(starts))
    ]
    return header, blocks


def _sort_key(index_and_block: tuple[int, str]) -> tuple[int, int, int]:
    """Newest first. An unparseable heading sorts last but is never dropped."""
    index, block = index_and_block
    match = DAY_HEADING.match(block.split("\n", 1)[0])
    if not match:
        return (0, 0, -index)
    day, month = int(match.group(1)), int(match.group(2))
    return (1, month * 100 + day, -index)


def recent_blocks(blocks: list[str], count: int) -> list[str]:
    """The `count` most recent day blocks, by heading date, file order ignored."""
    ordered = sorted(enumerate(blocks), key=_sort_key, reverse=True)
    return [block for _, block in ordered[: max(0, count)]]


def render(header: str, blocks: list[str]) -> str:
    """Deterministic."""
    parts = [header.rstrip()] if header.strip() else []
    parts += [block.rstrip() for block in blocks]
    return ("\n\n".join(parts) + "\n") if parts else ""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Emit the recent part of work/log.md."
    )
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--recent", type=int, default=DEFAULT_RECENT)
    parser.add_argument(
        "--to-string",
        action="store_true",
        help="return the text instead of printing it (used by the suite)",
    )
    args = parser.parse_args(argv)

    root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[1]
    )
    path = root / LOG_PATH
    if not path.is_file():
        # Phase 1 creates it from a template and continues; do not die here.
        return "" if args.to_string else 0

    header, blocks = parse_blocks(path.read_text(encoding="utf-8", errors="replace"))
    out = render(header, recent_blocks(blocks, args.recent))
    if args.to_string:
        return out
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
