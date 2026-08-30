#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pytest suite for scripts/bridge-config.py.

CONTRACT, this file is the authoritative spec for that surface.

WHY THIS EXISTS. `bridge-config.yaml` holds twenty-one top-level blocks, and
Phase 1 needs six of them. Telling an agent to "read the theme and work keys"
saves nothing, because reading a file reads the file: the saving is only real if
something emits the slice. This does.

The other fifteen blocks belong to the skill that owns them, which reads its own
block when it runs. That is the same rule the repo already applies to skills and
registries; the session load was the one place it had never been applied.

    SESSION_KEYS
        The blocks Phase 1 genuinely needs. Deliberately a constant in CORE and
        not a config value: what the session load requires is a CORE decision,
        and a config file that declares how much of itself to read is a loop.

    session_slice(config) -> dict
        Those keys, in SESSION_KEYS order, skipping the ones the config does
        not carry. An absent block is absent, never invented and never a crash:
        most of them are optional by design.

    named_slice(config, keys) -> (dict, list[str])
        The requested keys plus the ones that were not there. A skill asking
        for a block it owns should get an empty answer and a note, not a
        traceback and not silence.

    render(slice) -> str
        Deterministic YAML. The same config renders the same bytes twice, so
        the payload can be measured and capped like any other always-on item.

    main(argv) -> int
        `--session`, or `--keys a,b`. No config file is not an error: a fresh
        open-bridge clone has none, and the session load must still work.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bridge-config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bridge_config", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bridge_config"] = mod
    spec.loader.exec_module(mod)
    return mod


bc = _load_module()

CONFIG = """
identity:
  home: /home/someone
theme: professional
language:
  conversation: en
work:
  enabled: true
  max_active: 7
promote:
  scrub_rules: {}
remotes:
  default: somewhere
models:
  default: a-model
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "bridge-config.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------ the slice --

def test_the_session_slice_carries_only_the_session_keys(tree):
    got = bc.session_slice(bc.load_config(tree))
    assert set(got) <= set(bc.SESSION_KEYS)
    assert "work" in got and "theme" in got


def test_the_session_slice_leaves_the_skill_owned_blocks_behind(tree):
    got = bc.session_slice(bc.load_config(tree))
    for owned in ("promote", "remotes", "models"):
        assert owned not in got


def test_an_absent_session_key_is_absent_not_invented(tree):
    got = bc.session_slice(bc.load_config(tree))
    assert "purpose" not in got


def test_the_slice_keeps_the_values_intact(tree):
    got = bc.session_slice(bc.load_config(tree))
    assert got["work"]["max_active"] == 7


# ------------------------------------------------------- the named slice --

def test_named_slice_returns_what_was_asked_for(tree):
    got, missing = bc.named_slice(bc.load_config(tree), ["remotes", "theme"])
    assert set(got) == {"remotes", "theme"}
    assert missing == []


def test_named_slice_reports_a_key_that_is_not_there(tree):
    """A skill asking for a block it owns gets an empty answer and a note,
    never a traceback and never silence."""
    got, missing = bc.named_slice(bc.load_config(tree), ["nope"])
    assert got == {}
    assert missing == ["nope"]


# ----------------------------------------------------------- the render --

def test_the_render_is_byte_identical_across_runs(tree):
    config = bc.load_config(tree)
    assert bc.render(bc.session_slice(config)) == bc.render(bc.session_slice(config))


def test_the_render_round_trips(tree):
    config = bc.load_config(tree)
    slice_ = bc.session_slice(config)
    assert yaml.safe_load(bc.render(slice_)) == slice_


def test_the_render_is_smaller_than_the_file_it_came_from(tree):
    """The point of the exercise, asserted rather than assumed."""
    whole = (tree / "bridge-config.yaml").read_text(encoding="utf-8")
    assert len(bc.render(bc.session_slice(bc.load_config(tree)))) < len(whole)


# ------------------------------------------------------------- the main --

def test_session_exits_zero_and_prints(tree, capsys):
    assert bc.main(["--session", "--repo-root", str(tree)]) == 0
    assert "max_active" in capsys.readouterr().out


def test_keys_exits_zero_and_prints_only_those(tree, capsys):
    assert bc.main(["--keys", "remotes", "--repo-root", str(tree)]) == 0
    out = capsys.readouterr().out
    assert "remotes" in out and "max_active" not in out


def test_no_config_file_is_not_an_error(tmp_path, capsys):
    """A fresh open-bridge clone has none, and the session load must still run."""
    assert bc.main(["--session", "--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == ""
