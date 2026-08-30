#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Progressive disclosure for standing orders: the index always, bodies on demand.

Skills already work this way. The name and the description sit in context
permanently; the body arrives when the skill is invoked. Standing orders never
got the same treatment: every order carrying `scope: always` is read in full at
session start and matched against every sub-agent dispatch, so an advisory
order that fires twice a month is paid on every turn and again on every fan-out.

This adds the missing half of the skill contract to orders. Three fields:

    load      eager | on-trigger      default: eager
    triggers  the vocabulary that fetches the body
    summary   the one line that stays in context

    python3 scripts/standing-orders.py --index    # the always-on surface
    python3 scripts/standing-orders.py --check    # validate the contract

THE DEFAULT IS EAGER, and that direction is the safety argument. A fork that
has never heard of the field keeps every order it has and loses nothing; it
simply saves nothing either. Fail-closed would mean a guardrail going quiet
because somebody had not migrated a file yet.

WHAT THE CHECK IS ACTUALLY FOR. An order that says `on-trigger` but names no
trigger can never be fetched, and one with no summary never appears in the
index, so nothing ever asks for it. Either way the file sits in the tree
reading as enforced while being absent from every session. That is a worse
failure than the cost this change removes, so it is refused rather than warned
about.

The contract lives in `scripts/lib/standing_orders.py`, shared with
`scripts/measure-context.py` so the two can never disagree about what is
always-on. The spec is `scripts/tests/test_standing_orders.py`.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.standing_orders import (  # noqa: E402  (path setup must precede this import)
    DEFAULT_LOAD,
    ORDERS_DIR,
    SUMMARY_MAX_CHARS,
    VALID_LOAD,
    check_orders,
    collect_orders,
    eager_paths,
    load_order,
    render_index,
    unreadable_orders,
)

__all__ = [
    "DEFAULT_LOAD", "ORDERS_DIR", "SUMMARY_MAX_CHARS", "VALID_LOAD",
    "check_orders", "collect_orders", "eager_paths", "load_order",
    "render_index", "unreadable_orders", "main",
]

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Standing-order index and contract check."
    )
    parser.add_argument("--repo-root", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--index", action="store_true", help="print the always-on index")
    mode.add_argument("--check", action="store_true", help="validate the contract")
    args = parser.parse_args(argv)

    root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[1]
    )
    orders = collect_orders(root)

    if args.check:
        violations = check_orders(orders, unreadable_orders(root))
        if violations:
            print("standing-orders: contract violations", file=sys.stderr)
            for violation in violations:
                print(f"  {violation}", file=sys.stderr)
            return 1
        eager = len(eager_paths(orders))
        print(
            f"standing-orders: {len(orders)} order(s) valid "
            f"({eager} eager, {len(orders) - eager} on-trigger)"
        )
        return 0

    print(render_index(orders))
    return 0


if __name__ == "__main__":
    sys.exit(main())
