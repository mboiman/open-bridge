"""Prereq 2 — durable per-(item,stage) retry counter.

`tick.py` used to treat `stage.retry` as a boolean: a stage with `retry>0` that
kept failing was left in place and re-dispatched FOREVER, so the configured
`on_fail` (rewind/park) was unreachable — dead config. The fix is a real attempt
counter, fsync-persisted (clone of the TokenBudget pattern), that EXHAUSTS into
rewind/park after `retry` failures and survives a crash/lock-reclaim.
"""
from engine.board import FakeBoardClient
from engine.config import EngineConfig
from engine.guards import AttemptCounter
from engine.interfaces import BoardItem, StageResult, Stage
from engine.runner import FakeStageRunner
from engine.tick import Engine


def _pipeline(on_fail="park", rewind_to=None):
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(
            id="implement",
            run="cmd:true",
            on_success="implementing",
            retry=2,
            on_fail=on_fail,
            rewind_to=rewind_to,
        ),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def test_retry_exhausts_to_park_after_n_failures(tmp_path):
    board = FakeBoardClient(
        items=[BoardItem(id="A", title="x", status="Doing", pipeline="specced")]
    )
    calls = {"n": 0}

    def always_fail(stage, item):
        if stage.id == "implement":
            calls["n"] += 1
            return StageResult(ok=False, notes="flaky")
        return StageResult(ok=True)

    runner = FakeStageRunner(board=board)
    runner.run = always_fail
    cfg = EngineConfig(stages=_pipeline(on_fail="park"), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    for _ in range(10):
        engine.tick()

    # retry=2 → 1 initial run + 2 retries = 3 runs, THEN park. Not infinite.
    assert calls["n"] == 3
    assert board.pipeline_of("A") == "parked"


def test_retry_exhausts_to_rewind(tmp_path):
    board = FakeBoardClient(
        items=[BoardItem(id="A", title="x", status="Doing", pipeline="specced")]
    )
    seq = {"n": 0}

    def fail_implement(stage, item):
        if stage.id == "implement":
            seq["n"] += 1
            return StageResult(ok=False, notes="flaky")
        return StageResult(ok=True)

    runner = FakeStageRunner(board=board)
    runner.run = fail_implement
    cfg = EngineConfig(stages=_pipeline(on_fail="rewind", rewind_to="specced"), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    # tick once per failure: 2 retries are absorbed (pipeline stays), the 3rd exhausts → rewind
    engine.tick()
    assert board.pipeline_of("A") == "specced"  # stays at its before-key while retries remain
    engine.tick()
    assert board.pipeline_of("A") == "specced"
    res = engine.tick()  # exhaust → rewind (here back to its own before-key, no forward move)
    assert seq["n"] == 3
    # exhausted: routed through on_fail rewind, not left looping silently
    assert board.pipeline_of("A") == "specced"
    assert "A" not in res.parked


def test_attempt_counter_is_durable_across_instances(tmp_path):
    state = tmp_path / "attempts.json"
    c1 = AttemptCounter(state_file=state)
    assert c1.get("A::implement") == 0
    assert c1.bump("A::implement") == 1
    assert c1.bump("A::implement") == 2
    # a fresh instance (crash / new tick process) must SEE the persisted count
    c2 = AttemptCounter(state_file=state)
    assert c2.get("A::implement") == 2
    c2.reset("A::implement")
    c3 = AttemptCounter(state_file=state)
    assert c3.get("A::implement") == 0
