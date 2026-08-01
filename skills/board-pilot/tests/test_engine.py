"""Engine guards + idempotency + safety."""
import pytest

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


def _engine(tmp_path, items, prs=None, **cfg):
    board = FakeBoardClient(items=items, prs=prs)
    runner = FakeStageRunner(board=board)
    config = EngineConfig(stages=pipeline(), trigger_status="Todo", **cfg)
    return Engine(config, board, runner, state_dir=tmp_path), board, runner


def reject_pipeline():
    """The same chain, but `review` can send an item back to `implement`.

    The blind-rework guard only means anything where a reject edge exists: that is
    the only edge that produces a note, so it is the only one whose ABSENT note is
    evidence of a lost one.
    """
    return [
        Stage(id="spec", run="cmd:true", on_success="implementing"),
        Stage(id="implement", run="cmd:true", on_success="verifying"),
        Stage(id="verify", run="cmd:true", on_success="reviewing"),
        Stage(id="review", run="cmd:true", on_success="pr-ready", reject_to="implement"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def _reject_engine(tmp_path, items, prs=None, **cfg):
    board = FakeBoardClient(items=items, prs=prs)
    runner = FakeStageRunner(board=board)
    config = EngineConfig(
        stages=reject_pipeline(), trigger_status="Todo", max_rounds=3, **cfg
    )
    return Engine(config, board, runner, state_dir=tmp_path), board, runner


def test_does_not_arm_outside_trigger_column(tmp_path):
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Backlog")])
    for _ in range(5):
        engine.tick()
    assert runner.total_calls == 0
    assert board.pipeline_of("A") is None


def test_pr_idempotent_when_pr_already_exists(tmp_path):
    # a PR already exists (e.g. a crash between opening it and writing the board)
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")], prs={"A"})
    for _ in range(10):
        engine.tick()
    assert runner.pr_create_calls == 0            # never opens a second PR
    assert "pr" not in runner.ran_stage_ids       # the pr stage handler is skipped
    assert board.pipeline_of("A") == "pr-open"
    assert board.status_of("A") == "In Review"


def test_paused_file_halts_everything(tmp_path):
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    (engine.paused_file).write_text("stop")
    res = engine.tick()
    assert res.paused is True
    assert runner.total_calls == 0
    assert board.pipeline_of("A") is None


def test_malicious_item_id_is_parked_not_executed(tmp_path):
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A; rm -rf ~", title="x", status="Todo")])
    for _ in range(5):
        engine.tick()
    assert runner.total_calls == 0
    assert str(board.pipeline_of("A; rm -rf ~")).startswith("parked")


def test_token_ceiling_halts_before_pr(tmp_path):
    engine, board, runner = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Todo")], token_ceiling=250
    )
    for _ in range(10):
        engine.tick()
    # 100 tokens/stage, ceiling 250 → halts after spec+implement+verify (300 ≥ 250)
    assert runner.pr_create_calls == 0
    assert len(runner.ran_stage_ids) < 5
    assert "pr" not in runner.ran_stage_ids


def test_wiped_snapshot_does_not_rearm_inflight_item(tmp_path):
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    r1 = engine.tick()                 # arms A + runs spec
    assert "A" in r1.armed
    engine.prev_path.unlink()          # simulate a wiped snapshot
    r2 = engine.tick()                 # must NOT re-arm (pipeline field is the durable marker)
    assert "A" not in r2.armed
    assert runner.ran_stage_ids == ["spec", "implement"]  # progressed once, no duplicate


def test_board_write_error_does_not_crash_tick(tmp_path):
    """A board/gh write failure (e.g. 403 before `gh auth refresh -s project`) must NOT
    crash the poll loop — the item is skipped with a one-line note; the next tick retries.
    Otherwise the unit tracebacks to err.log every 60s until scope is granted."""
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])

    def boom(*a, **k):
        raise RuntimeError("gh failed (403): needs project scope")

    board.set_pipeline = boom
    res = engine.tick()                   # must NOT raise
    assert "A" in res.skipped
    assert "403" in res.notes
    assert runner.total_calls == 0        # never reached a stage handler


def test_stage_failure_parks_with_bare_board_option(tmp_path):
    """A non-retryable stage failure must park with the BARE 'parked' value (a live board
    single-select option), never a compound 'parked:fail' the board can't store — and the
    parked item must be terminal (never re-dispatched, so a blocked push / hollow analysis
    does not loop a real claude run every tick). The reason rides in notes."""
    from engine.interfaces import StageResult

    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    calls = {"n": 0}

    def failing_run(stage, item):
        calls["n"] += 1
        return StageResult(ok=False, notes="boom: push rejected (ruleset)")

    runner.run = failing_run

    r1 = engine.tick()                       # arm A (queued) + run first stage → fails → park
    assert board.pipeline_of("A") == "parked"        # exact bare option, NOT "parked:fail"
    assert "A" in r1.parked
    assert "park A" in r1.notes and "boom" in r1.notes   # failure reason surfaced in notes
    n_after = calls["n"]
    for _ in range(3):
        engine.tick()                        # parked = terminal → excluded by _is_inflight
    assert calls["n"] == n_after             # the stage handler never ran again


# === spec row 3: blind-rework park =========================================


def test_bounced_item_without_note_parks(tmp_path):
    """A rework round with NO feedback attached must park, not re-run the producer.

    `bounces > 0` means a reviewer sent this item back; an empty annotation means the
    note never reached it — the comment write failed, or the authenticated read-back
    denied it. Re-dispatching now asks a model to redo work while telling it nothing
    about what was wrong: it produces the same output, gets rejected again, and burns
    the entire rework budget producing nothing. Worse, the ledger reports a healthy
    reject loop the whole time, so the failure is invisible. Park instead — it is the
    same information, made loud, for zero spend.
    """
    engine, board, runner = _reject_engine(
        tmp_path, [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )
    board.set_number("A", "Bounces", 1)   # the round landed on the durable counter...
    # ...but no round-1 note is on the issue, so fetch_items reads annotation ""

    res = engine.tick()

    assert board.pipeline_of("A") == "parked"
    assert "A" in res.parked
    assert "A" in str(res.notes) and "note" in res.notes   # names WHY, not just "parked"
    assert runner.total_calls == 0        # the whole point: zero spend on a blind rework


def test_bounced_item_with_note_still_dispatches(tmp_path):
    """The negative path. A guard that parks every bounced item would kill the reject
    edge outright — and would still pass the test above."""
    engine, board, runner = _reject_engine(
        tmp_path, [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )
    board.set_number("A", "Bounces", 1)
    board.comment("A", "<!-- board-pilot:reject round=1 -->\nadd a test for the empty case")

    res = engine.tick()

    assert board.pipeline_of("A") == "verifying"   # advanced normally
    assert runner.ran_stage_ids == ["implement"]
    assert "A" in res.dispatched


def test_unbounced_item_without_note_dispatches(tmp_path):
    """An item that was never rejected has no note by definition — it must not park."""
    engine, board, runner = _reject_engine(
        tmp_path, [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )

    res = engine.tick()

    assert board.pipeline_of("A") == "verifying"
    assert "A" in res.dispatched


def test_bounced_item_without_note_does_not_park_without_a_reject_edge(tmp_path):
    """The scoping that keeps the two backward edges from cancelling each other out.

    Both edges bump the SAME durable counter, but only a reject writes a note — a
    rewind is crash recovery, with no reviewer and nothing to say. So on a chain with
    no reject edge, `bounces > 0` + empty note is the NORMAL state of a rewound item,
    not a lost note. An ungated guard reads it as a blind rework and parks on the
    first crash, silently disabling the rewind edge and its cap.
    """
    engine, board, runner = _engine(   # note: `pipeline()`, no reject edge
        tmp_path, [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )
    board.set_number("A", "Bounces", 1)

    res = engine.tick()

    assert board.pipeline_of("A") != "parked"
    assert "A" in res.dispatched


# === spec row 3: the human-Done guard ======================================


def test_human_done_halts_dispatch(tmp_path):
    """A human setting Done mid-flight must STOP the engine — not be overwritten.

    `_is_inflight` reads only `pipeline`, never `status`, so a card a person marked
    Done still reaches the gate, opens a PR, and gets its Status stamped back to
    `pr_status`. "The engine never SETS done_status" was never the same promise as
    "the engine STOPS when a human sets it". The human wins, always.
    """
    engine, board, runner = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Done", pipeline="pr-ready")]
    )

    res = engine.tick()

    assert board.status_of("A") == "Done"     # the terminal human signal survives
    assert runner.total_calls == 0            # the gated PR stage never ran
    assert runner.pr_create_calls == 0
    assert "A" not in res.dispatched


def test_human_done_halts_dispatch_mid_pipeline(tmp_path):
    """Not just at the gate: Done must halt an item at any stage, before any spend."""
    engine, board, runner = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Done", pipeline="implementing")]
    )

    res = engine.tick()

    assert board.status_of("A") == "Done"
    assert board.pipeline_of("A") == "implementing"   # left exactly where the human found it
    assert runner.total_calls == 0
    assert "A" not in res.dispatched


def test_human_done_halt_is_not_a_park(tmp_path):
    """Done is the human's terminal state, not the engine's failure state.

    Parking would overwrite a deliberate human decision with an engine verdict and
    make a finished card look broken on the board.
    """
    engine, board, runner = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Done", pipeline="implementing")]
    )

    res = engine.tick()

    assert board.pipeline_of("A") != "parked"
    assert "A" not in res.parked


def test_human_done_halt_does_not_consume_concurrency(tmp_path):
    """No work was done, so no slot is spent — otherwise one Done card left on the
    board would starve a `concurrency: 1` pipeline forever."""
    engine, board, runner = _engine(
        tmp_path,
        [
            BoardItem(id="DONE", title="done", status="Done", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="implementing"),
        ],
        concurrency=1,
    )

    res = engine.tick()

    assert "GOOD" in res.dispatched
    assert runner.ran_stage_ids == ["implement"]


# === the option preflight, wired at construction ===========================


class _PreflightBoard(FakeBoardClient):
    """A board that records the preflight call, like the real GhBoardClient answers it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.preflight_calls = []

    def preflight_options(self, pipeline_values, status_values=()):
        self.preflight_calls.append((list(pipeline_values), list(status_values)))


def test_engine_preflights_every_writable_pipeline_option(tmp_path):
    """`preflight_options` existed but nothing called it — a guard that never runs.

    `_option_id` KeyErrors on any value the board does not carry, and that raise lands
    inside the dispatch loop where the outer guard swallows it into a skip: the item
    never advances and the expensive stage re-dispatches every poll, forever. The
    board is hand-configured, so a missing option is the EXPECTED first-run state.
    """
    board = _PreflightBoard(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=pipeline(), trigger_status="Todo")

    Engine(cfg, board, runner, state_dir=tmp_path)

    assert len(board.preflight_calls) == 1        # exactly once, at construction
    pipeline_values, _status = board.preflight_calls[0]
    assert "queued" in pipeline_values            # the ARM latch
    assert "parked" in pipeline_values            # every park site writes the bare option
    for value in ("implementing", "verifying", "reviewing", "pr-ready", "pr-open"):
        assert value in pipeline_values           # every forward edge, incl. the terminal


def test_engine_preflights_writable_status_values(tmp_path):
    """pr_status / working_status / park_status are engine-written too, and a missing
    option wedges them the same way."""
    board = _PreflightBoard(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(
        stages=pipeline(),
        trigger_status="Todo",
        pr_status="In Review",
        working_status="In Progress",
        park_status="Blocked",
    )

    Engine(cfg, board, runner, state_dir=tmp_path)

    _pipeline_values, status_values = board.preflight_calls[0]
    assert set(status_values) == {"In Review", "In Progress", "Blocked"}


def test_engine_preflight_omits_unconfigured_status_values(tmp_path):
    """working_status / park_status default to None (today's engine writes neither).

    Preflighting a value the engine never writes would demand a board option for
    nothing and fail startup on a config that works.
    """
    board = _PreflightBoard(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=pipeline(), trigger_status="Todo", pr_status="In Review")

    Engine(cfg, board, runner, state_dir=tmp_path)

    _pipeline_values, status_values = board.preflight_calls[0]
    assert status_values == ["In Review"]
    assert None not in status_values


def test_engine_preflights_the_rewind_target(tmp_path):
    """A rewind target is a pipeline value the engine WRITES — so it must be live."""
    board = _PreflightBoard(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board)
    stages = [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(id="verify", run="cmd:true", on_success="verifying",
              on_fail="rewind", rewind_to="specced"),
        Stage(id="pr", run="cmd:true", on_success="pr-open", gate="human"),
    ]
    cfg = EngineConfig(stages=stages, trigger_status="Todo")

    Engine(cfg, board, runner, state_dir=tmp_path)

    pipeline_values, _status = board.preflight_calls[0]
    assert "specced" in pipeline_values


def test_engine_preflight_failure_raises_at_construction(tmp_path):
    """The whole point is failing BEFORE any spend. A preflight that raises must take
    the constructor down, not be swallowed into a skip at dispatch time."""
    class _Missing(FakeBoardClient):
        def preflight_options(self, pipeline_values, status_values=()):
            raise RuntimeError("field 'Pipeline' does not exist on the board")

    board = _Missing(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=pipeline(), trigger_status="Todo")

    with pytest.raises(RuntimeError) as ei:
        Engine(cfg, board, runner, state_dir=tmp_path)
    assert "Pipeline" in str(ei.value)
    assert runner.total_calls == 0


def test_engine_preflight_options_is_duck_typed(tmp_path):
    """A board client with no preflight (the in-memory Fake — there is no live board
    to check against) must construct fine, exactly like preflight_reject_field."""
    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    assert engine.tick().armed == ["A"]


def test_live_lock_blocks_dispatch_no_double_run(tmp_path):
    """A live worker holding the item lock → the next tick must NOT re-run the
    stage. This is the concurrency/double-dispatch case the happy-path acceptance
    test cannot see (and the case the inverted-liveness bug used to break)."""
    import json
    import os
    import time

    engine, board, runner = _engine(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    engine.lockdir.mkdir(parents=True, exist_ok=True)
    (engine.lockdir / "A.lock").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": time.time()})
    )
    res = engine.tick()
    assert "A" in res.skipped       # lock held by a live worker → skipped
    assert runner.total_calls == 0  # the stage did NOT run a second time
