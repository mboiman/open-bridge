"""THE GOAL — a successful test of the board-pilot process up to the pull request.

A board item dragged into `Todo` is driven autonomously through every stage to a
pull request: it opens exactly one (draft) PR, lands in `In Review`, and the
engine STOPS at the human merge gate — it never sets `Done`. Subsequent ticks are
pure no-ops.
"""
from engine.board import FakeBoardClient
from engine.config import EngineConfig
from engine.interfaces import BoardItem, Stage
from engine.runner import FakeStageRunner
from engine.tick import Engine


def pipeline():
    return [
        Stage(id="spec", run="cmd:true", on_success="implementing"),
        Stage(id="implement", run="cmd:true", on_success="verifying"),
        Stage(id="verify", run="cmd:true", on_success="reviewing"),
        Stage(id="review", run="cmd:true", on_success="pr-ready"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def reject_pipeline():
    # before-keys: spec=queued, implement=specced, verify=implementing,
    # review=verifying, pr=reviewing.
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(id="verify", run="cmd:true", on_success="verifying"),
        Stage(id="review", run="cmd:true", on_success="reviewing", reject_to="implement", max_rounds=2),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def test_process_reaches_pr_and_stops(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="ITEM-1", title="eml ingest", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=pipeline(), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    for _ in range(10):
        engine.tick()

    # every stage ran, in order
    assert runner.ran_stage_ids == ["spec", "implement", "verify", "review", "pr"]
    # exactly one PR, idempotent
    assert runner.pr_create_calls == 1
    # terminal engine state + handed to human review
    assert board.pipeline_of("ITEM-1") == "pr-open"
    assert board.status_of("ITEM-1") == "In Review"
    # the engine NEVER auto-completes
    assert board.status_of("ITEM-1") != cfg.done_status
    # stopped at the gate — more ticks change nothing
    before = runner.total_calls
    engine.tick()
    assert runner.total_calls == before


def test_item_rejected_once_still_reaches_pr(tmp_path):
    """THE GOAL holds across one reject round: an item bounced back to the producer
    once is reworked and STILL reaches a single draft PR + In Review, never Done."""
    board = FakeBoardClient(items=[BoardItem(id="ITEM-1", title="eml ingest", status="Todo")])
    runner = FakeStageRunner(board=board, reject_stage="review", reject_rounds=1, reject_to="implement")
    cfg = EngineConfig(stages=reject_pipeline(), trigger_status="Todo", max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    for _ in range(20):
        engine.tick()

    assert board.bounces_of("ITEM-1") == 1            # exactly one reject round
    assert runner.ran_stage_ids.count("implement") >= 2  # producer re-ran after the bounce
    assert runner.pr_create_calls == 1                # exactly ONE PR despite the bounce
    assert board.pipeline_of("ITEM-1") == "pr-open"
    assert board.status_of("ITEM-1") == "In Review"
    assert board.status_of("ITEM-1") != cfg.done_status
