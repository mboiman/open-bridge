"""Prereq 1 — regression guard for the EXISTING failure-coupled `rewind` backward edge.

`tick.py` already carries one backward edge: a non-retryable stage failure with
`on_fail: rewind` + `rewind_to` sends the item back to an earlier pipeline state
(the before-key of an upstream producer). It was fully wired but had ZERO coverage;
the reject-and-return edge builds structurally on this exact path, so pin it first.

Spec row 1 — THE TERMINATOR. The rewind edge had no bound of any kind: the local
attempt counter was RESET on the way through, the durable board counter was touched
only by the reject branch, and the token budget is inert (the runner hardcodes
tokens=0). Measured against the real engine before the fix: **40 LLM runs in 40
ticks** — one paid run per tick, forever, `Bounces` stuck at 0, never parking. It
fires on day 1 rather than in some exotic corner: a stage script that does not exist
exits 127 → ok=False → rewind → loop.

The rewind PRIMITIVE stays in the engine and stays capped. The delivered stage chain
carries no rewind edge (a red test is rework and routes through the reject edge), but
the engine is generic and another project's config may use it — so the cap lives here,
not in the YAML.
"""
from engine.board import FakeBoardClient
from engine.config import EngineConfig
from engine.interfaces import BoardItem, Stage
from engine.runner import FakeStageRunner
from engine.tick import Engine


def _pipeline():
    # before-keys (chained): spec=queued, implement=specced, verify=implementing,
    # review=verifying, pr=reviewing; terminal=pr-open.
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(
            id="verify",
            run="cmd:true",
            on_success="verifying",
            on_fail="rewind",
            rewind_to="specced",  # send a failed verify back to implement's before-key
        ),
        Stage(id="review", run="cmd:true", on_success="reviewing"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def test_failed_verify_rewinds_to_upstream_producer(tmp_path):
    # seed the item already in-flight, parked right before `verify`
    board = FakeBoardClient(
        items=[BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_pipeline(), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert "verify" in runner.ran_stage_ids          # the verify stage actually ran
    assert board.pipeline_of("A") == "specced"        # rewound to implement's before-key
    assert board.pipeline_of("A") != "parked"         # rewind, NOT terminal park
    assert "A" not in res.parked


# === spec row 1: the terminator ============================================


def _armed_engine(tmp_path, max_rounds=None):
    """An item armed from the trigger column — the shape the defect probe measured."""
    board = FakeBoardClient(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_pipeline(), trigger_status="Todo", max_rounds=max_rounds)
    return Engine(cfg, board, runner, state_dir=tmp_path), board, runner


def test_rewind_edge_terminates(tmp_path):
    """The headline: a rewind-only config must PARK after N rounds, not loop forever.

    Measured before the fix, this exact scenario: 40 ticks → 40 LLM runs (spec x1,
    implement x20, verify x19), Bounces 0, pipeline still 'implementing'. The tick
    count was the only thing bounding the spend, and in production nothing bounds
    the tick count — the poller runs every 60s.
    """
    engine, board, runner = _armed_engine(tmp_path, max_rounds=2)

    for _ in range(40):
        engine.tick()

    assert board.pipeline_of("A") == "parked"   # terminated, on the bare board option
    assert board.bounces_of("A") == 2           # each rewind round counted, durably
    # spec → implement → verify(round 1) → implement → verify(round 2 = cap) → park.
    # The exact number matters: it is the difference between a bounded and an
    # unbounded bill. Anything that grows with tick count is the defect returning.
    assert runner.total_calls == 5
    assert runner.ran_stage_ids == ["spec", "implement", "verify", "implement", "verify"]


def test_rewind_counts_on_durable_bounce_field(tmp_path):
    """The counter must be the BOARD Number field, not process-local state.

    The local attempt counter cannot terminate this edge: it lives in the state dir
    and the rewind path used to `reset()` it on every pass, so it read 0 forever. The
    board field is the one counter a crash, a lock reclaim or a wiped state dir cannot
    rewind — which is exactly why the reject edge already uses it.
    """
    engine, board, runner = _armed_engine(tmp_path, max_rounds=3)

    seen = []
    for _ in range(6):
        engine.tick()
        seen.append(board.bounces_of("A"))

    # spec, implement, verify(1), implement, verify(2), implement → counter climbs
    # on the rewind ticks only, and never resets under the running item.
    assert seen == [0, 0, 1, 1, 2, 2]
    assert board.bounces_of("A") == 2

    for _ in range(20):
        engine.tick()
    assert board.bounces_of("A") == 3           # stops AT the cap, never past it
    assert board.pipeline_of("A") == "parked"


def test_rewind_cap_defaults_when_config_declares_no_max_rounds(tmp_path):
    """A rewind-only config with no `rework.max_rounds` must STILL terminate.

    `validate_chain` requires a positive max_rounds for a reject edge but says
    nothing about a rewind edge — so this config loads silently today and then spends
    without bound. An engine-side default is the only thing standing between a
    forgotten YAML key and an open-ended bill.
    """
    engine, board, runner = _armed_engine(tmp_path, max_rounds=None)

    for _ in range(40):
        engine.tick()

    assert board.pipeline_of("A") == "parked"
    assert board.bounces_of("A") == 3           # the engine-side default cap
    assert runner.total_calls == 7              # spec + 3x(implement+verify)


def test_rewind_bounce_write_failure_parks_fail_closed(tmp_path):
    """If the durable counter cannot be written, PARK — never rewind uncounted.

    Falling through to the generic skip-and-retry would re-run the crashed stage on
    every poll with nothing counting the rounds: the original defect, reached by a
    different door. The stage already failed, so there is no rework budget worth
    preserving through a transient board error — fail closed.
    """
    engine, board, runner = _armed_engine(tmp_path, max_rounds=3)
    engine.tick()   # arm + spec
    engine.tick()   # implement

    def boom(*a, **k):
        raise RuntimeError("gh failed (403): needs project scope")

    board.get_number = boom
    board.set_number = boom

    res = engine.tick()   # verify crashes → rewind edge → counter write fails

    assert board.pipeline_of("A") == "parked"
    assert "A" in res.parked
    assert "403" in res.notes            # the operator learns WHY, not just "parked"
    calls = runner.total_calls
    for _ in range(5):
        engine.tick()
    assert runner.total_calls == calls   # terminal: the crashed stage never re-runs


def test_rewind_only_config_runs_the_bounce_field_preflight(tmp_path):
    """The bounce preflight must fire for a rewind-only config too.

    It used to key off the reject edge alone, so a config whose only backward edge is
    a rewind got no preflight at all — and now that the rewind edge depends on the
    same durable Number field, a missing field would KeyError inside the dispatch
    loop, be swallowed into a skip, and re-run the expensive stage every poll. That is
    the failure the preflight exists to convert into a loud startup error.
    """
    seen = []

    class BoardWithPreflight(FakeBoardClient):
        def preflight_reject_field(self, field_name=None):
            seen.append(field_name)

    board = BoardWithPreflight(items=[BoardItem(id="A", title="x", status="Todo")])
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_pipeline(), trigger_status="Todo", bounce_field="Bounces")

    Engine(cfg, board, runner, state_dir=tmp_path)

    assert seen == ["Bounces"]


def test_rewind_only_config_resets_the_bounce_counter_at_arm(tmp_path):
    """A re-armed item starts a fresh rework budget — for the rewind edge as well.

    Without this a card that parked at the cap, then got dragged back to the trigger
    column by a human, would arrive already exhausted and park again on its first
    crash, looking identical to a card that had used up its rounds honestly.
    """
    engine, board, runner = _armed_engine(tmp_path, max_rounds=2)
    for _ in range(40):
        engine.tick()
    assert board.pipeline_of("A") == "parked"
    assert board.bounces_of("A") == 2

    # a human clears the pipeline and re-drags the card into the trigger column
    board.set_pipeline("A", None)
    board.set_status("A", "Todo")
    engine.tick()

    assert board.bounces_of("A") == 0
