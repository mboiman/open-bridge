#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Emit the slice of bridge-config.yaml a session actually needs.

`bridge-config.yaml` holds twenty-one top-level blocks. Phase 1 needs six.
Telling an agent to "read the theme and work keys" saves nothing, because
reading a file reads the file: the saving is only real if something emits the
slice. This does.

    python3 scripts/bridge-config.py --session      # what Phase 1 loads
    python3 scripts/bridge-config.py --keys remotes # what a skill loads

The other fifteen blocks belong to the skill that owns them, which reads its own
block when it runs. That is the rule this repo already applies to skills and
registries; the session load was the one place it had never been applied.

No config file is not an error. A fresh open-bridge clone has none, and the
session load must still work.

The contract lives in `scripts/tests/test_bridge_config.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

CONFIG_NAME = "bridge-config.yaml"

# The blocks Phase 1 genuinely needs, in the order they are emitted.
#
# Deliberately a constant in CORE rather than a config value: what the session
# load requires is a CORE decision, and a config file that declares how much of
# itself to read is a loop. Adding a block here is a reviewed change, which is
# the point, because every addition is paid on every turn.
SESSION_KEYS = (
    "identity",      # ${variable} interpolation across every cluster wrapper
    "purpose",       # opens the session oriented on the instance's north star
    "user_profile",  # which lane the instance was onboarded into
    "theme",         # user-facing vocabulary
    "language",      # conversation + artifact language
    "work",          # task management on/off, WIP cap, logging level
)


def load_config(repo_root: Path) -> dict:
    """The parsed config, or an empty dict when there is none."""
    path = Path(repo_root) / CONFIG_NAME
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def session_slice(config: dict) -> dict:
    """The session keys the config carries, in SESSION_KEYS order.

    An absent block is absent: never invented, never a crash. Most of them are
    optional by design.
    """
    return {key: config[key] for key in SESSION_KEYS if key in config}


def named_slice(config: dict, keys) -> tuple[dict, list[str]]:
    """The requested keys, plus the ones that were not there.

    A skill asking for a block it owns should get an empty answer and a note,
    not a traceback and not silence.
    """
    wanted = [k.strip() for k in keys if k.strip()]
    got = {key: config[key] for key in wanted if key in config}
    missing = [key for key in wanted if key not in config]
    return got, missing


def render(data: dict) -> str:
    """Deterministic YAML, so the payload can be measured and capped."""
    if not data:
        return ""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit the slice of bridge-config.yaml a caller needs."
    )
    parser.add_argument("--repo-root", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session", action="store_true")
    mode.add_argument("--keys", nargs="+", metavar="KEY")
    args = parser.parse_args(argv)

    root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[1]
    )
    config = load_config(root)

    if args.session:
        sys.stdout.write(render(session_slice(config)))
        return 0

    got, missing = named_slice(config, args.keys)
    if missing:
        print(
            f"bridge-config: no such block(s): {', '.join(missing)}",
            file=sys.stderr,
        )
    sys.stdout.write(render(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
