"""The CLI is the only place the transparency layer becomes REAL.

Every other test in this suite constructs ClaudeStageRunner by hand and passes
`evidence_dir=` / `criteria_dir=` itself. That proves the runner honours them —
and proves NOTHING about production, where `engine.cli` is the sole constructor.
Both parameters default to None, so a CLI that forgets them yields a fully green
suite over a feature that never fires: no tee, `criteria_ref` always None, and
every `criteria:` stage failing closed on "criteria_dir is not wired through".

So these tests drive `cli.run()` end-to-end and inspect what it ACTUALLY handed
the runner. The spy replaces the runner class rather than wrapping an instance:
cli imports it inside `run()`, so the module attribute is the real seam.
"""
from pathlib import Path

import pytest

from engine import cli

yaml = pytest.importorskip("yaml")


class _RunnerSpy:
    """Captures the ctor kwargs cli used. Never runs a stage."""

    last: "dict | None" = None

    def __init__(self, *args, **kwargs):
        type(self).last = kwargs
        self.log = []

    def run(self, stage, item):  # pragma: no cover - a tick never gets this far
        raise AssertionError("the Engine is stubbed; no stage may be dispatched")


class _BoardStub:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _EngineStub:
    """Stops the tick at construction — this file is about WIRING, not ticking."""

    last: "dict | None" = None

    def __init__(self, config, board, runner, state_dir, *args, **kwargs):
        type(self).last = {
            "config": config,
            "board": board,
            "runner": runner,
            "state_dir": state_dir,
        }

    def tick(self):
        from engine.tick import TickResult

        return TickResult()


_BASE = {
    "board": {
        "owner": "acme",
        "project_number": 1,
        "repo": "acme/widget",
    },
    "pipeline": {
        "project": "example",
        "trigger": {"on_status": "Todo"},
        "stages": [
            {"uses": "verify", "run": "cmd:make verify", "on_success": "verified"},
            {
                "uses": "pr",
                "run": "cmd:gh pr create --draft",
                "on_success": "pr-open",
                "gate": "human",
            },
        ],
    },
}


def _merge(**over) -> dict:
    """Deep-ish copy of _BASE with `board:`/`pipeline:` overlays."""
    import copy

    data = copy.deepcopy(_BASE)
    for block, patch in over.items():
        data[block].update(patch)
    return data


def _drive(tmp_path: Path, monkeypatch, data: dict) -> dict:
    """Run one real cli.run() over `data`; return the runner's ctor kwargs."""
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump(data), encoding="utf-8")
    state_dir = tmp_path / "state"

    monkeypatch.setattr("engine.claude_runner.ClaudeStageRunner", _RunnerSpy)
    monkeypatch.setattr("engine.gh_board.GhBoardClient", _BoardStub)
    monkeypatch.setattr("engine.tick.Engine", _EngineStub)
    _RunnerSpy.last = None

    rc = cli.run(["--project", str(project), "--state-dir", str(state_dir), "--once"])

    assert rc == 0
    assert _RunnerSpy.last is not None, "cli never constructed the runner"
    return _RunnerSpy.last


# -- the two parameters that were dead in production ----------------------
def test_cli_passes_evidence_dir_to_runner(tmp_path, monkeypatch):
    """THE test that would have caught wave 1's gap.

    `evidence.dir` is declared in the config; if cli does not forward it the
    runner's default None silently disables the tee, and the dossier's only
    machine-captured section never exists.
    """
    kw = _drive(
        tmp_path,
        monkeypatch,
        _merge(pipeline={"evidence": {"dir": "{state_dir}/evidence/{item_id}"}}),
    )

    assert kw.get("evidence_dir") is not None, (
        "cli constructed the runner without evidence_dir — the tee defaults off "
        "and NOTHING is captured in production"
    )


def test_cli_passes_criteria_dir_to_runner(tmp_path, monkeypatch):
    """Without this, every `criteria:` stage fails closed at
    claude_runner.py's "board.criteria_dir is not wired through" — the tuning
    knob is not merely inert, it parks the item."""
    kw = _drive(tmp_path, monkeypatch, _merge(board={"criteria_dir": "skills/board-pilot/criteria"}))

    assert kw.get("criteria_dir") == "skills/board-pilot/criteria"


def test_no_evidence_dir_configured_stays_none(tmp_path, monkeypatch):
    """The negative path. A knob that is always on would pass the two tests above
    while ignoring the config — no `evidence.dir` must mean no sink, not a
    guessed one.

    HONEST: this one was already GREEN before the wiring landed (cli passed
    neither parameter, so both read None). It is not a defect-proof; it is a
    ratchet against over-correcting the two above into a hardcoded default.
    """
    kw = _drive(tmp_path, monkeypatch, _merge())

    assert kw.get("evidence_dir") is None
    assert kw.get("criteria_dir") is None


# -- the caller/runner split ----------------------------------------------
def test_state_dir_substituted_by_caller_not_runner(tmp_path, monkeypatch):
    """`{state_dir}` is the CALLER's placeholder, `{item_id}` is the RUNNER's.

    Only cli knows --state-dir, and the runner renders per item. So cli must
    substitute exactly one placeholder and leave the other literal. Both ways of
    getting this wrong are silent-ish and are pinned here:

    * rendering nothing → the runner's `.format()` has no `state_dir` key, falls
      back to the raw template, and every item tees into a literal
      `{state_dir}` directory;
    * rendering with `.format(state_dir=…)` → KeyError on `item_id`, or, if
      someone "fixes" that by passing item_id too, every item on the board shares
      one evidence dir and overwrites its neighbours.
    """
    kw = _drive(
        tmp_path,
        monkeypatch,
        _merge(pipeline={"evidence": {"dir": "{state_dir}/evidence/{item_id}"}}),
    )
    tmpl = kw["evidence_dir"]

    assert "{state_dir}" not in tmpl, "cli left its OWN placeholder unrendered"
    assert str(tmp_path / "state") in tmpl, "the real --state-dir never reached the template"
    assert "{item_id}" in tmpl, (
        "cli rendered the runner's per-item placeholder — every item would then "
        "share one evidence dir"
    )


def test_runner_renders_what_cli_left_for_it(tmp_path, monkeypatch):
    """End-to-end on the split: feed cli's output to the REAL runner and prove
    the two halves compose into a per-item path. Each half is tested alone
    elsewhere; only their seam is tested here."""
    from engine.claude_runner import ClaudeStageRunner
    from engine.interfaces import BoardItem

    kw = _drive(
        tmp_path,
        monkeypatch,
        _merge(pipeline={"evidence": {"dir": "{state_dir}/evidence/{item_id}"}}),
    )

    runner = ClaudeStageRunner(
        repo="", branch_template="b/{item_id}", project="example", evidence_dir=kw["evidence_dir"]
    )
    item_dir = runner._evidence_item_dir(BoardItem(id="PVTI_abc", title="t", status="Todo"))

    assert item_dir == str(tmp_path / "state" / "evidence" / "PVTI_abc")
