"""The ARM gate: atomicity, the item lock, the issue requirement, the human column.

WHY THE ENGINE MAY WRITE THE HUMAN COLUMN AT ALL — the self-re-arm proof
------------------------------------------------------------------------
The old code refused to touch Status out of a re-arm fear: write the trigger value
back and the engine picks the card up again, forever. The fear is structurally void,
and this file pins the reason rather than trusting the prose.

Arming is a CONJUNCTION (tick.py): `i.status == trigger_status and not i.pipeline`.
Every pipeline write in the engine writes a non-empty value — "queued", "parked",
`stage.on_success` (validate_chain refuses an empty one), a resolved before-key, or
`stage.rewind_to` — and NOTHING clears the field. So the second conjunct is False for
the whole life of an armed item, and the status value is simply not load-bearing for
arming. `test_engine_status_write_cannot_rearm` drives the worst case that exists:
`working_status == trigger_status`, i.e. the engine writing the trigger value onto a
card it just armed. It still does not re-arm, because the pipeline latch — not the
column — is the gate.
"""
import pytest

from engine.board import FakeBoardClient
from engine.config import EngineConfig
from engine.interfaces import BoardItem, Stage, StageResult
from engine.lock import Lock
from engine.runner import FakeStageRunner
from engine.tick import Engine


def _pipeline():
    return [
        Stage(id="spec", run="cmd:true", on_success="implementing"),
        Stage(id="implement", run="cmd:true", on_success="verifying"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def _reject_pipeline():
    """Carries a reject edge, so `_has_backward_edge` is True and ARM resets Bounces."""
    return [
        Stage(id="spec", run="cmd:true", on_success="implementing"),
        Stage(id="implement", run="cmd:true", on_success="verifying"),
        Stage(id="review", run="cmd:true", on_success="pr-ready", reject_to="implement"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def _engine(tmp_path, items, stages=None, **cfg):
    board = FakeBoardClient(items=items)
    runner = FakeStageRunner(board=board)
    config = EngineConfig(stages=stages or _pipeline(), trigger_status="Todo", **cfg)
    return Engine(config, board, runner, state_dir=tmp_path), board, runner


def _arm_only(tmp_path, items, stages=None, **cfg):
    """`concurrency=0` → the dispatch loop breaks on its first iteration.

    One tick normally ARMS and then DISPATCHES the same item, and the dispatch path
    writes the human column too (the self-heal). Isolating the ARM is what makes
    these assertions mean what they say: without it, a missing ARM-time status write
    would be masked by the heal writing the same value microseconds later, and the
    test would pass against an engine that never implemented the feature.
    """
    return _engine(tmp_path, items, stages=stages, concurrency=0, **cfg)


def _spy_status(board):
    """Record every human-column write without performing it elsewhere."""
    calls = []
    real = board.set_status

    def spy(item_id, value):
        calls.append((item_id, value))
        real(item_id, value)

    board.set_status = spy
    return calls


def _boom(*_a, **_k):
    raise RuntimeError("gh failed (403): needs project scope")


# === working_status at ARM =================================================


def test_arm_writes_working_status(tmp_path):
    """Ask #4: a human must never pick up a story an agent already owns."""
    engine, board, _ = _arm_only(
        tmp_path, [BoardItem(id="A", title="x", status="Todo")], working_status="In Progress"
    )

    res = engine.tick()

    assert "A" in res.armed
    assert board.pipeline_of("A") == "queued"
    assert board.status_of("A") == "In Progress"


def test_arm_without_working_status_leaves_the_column_alone(tmp_path):
    """The knob defaults to None, and None must mean today's engine exactly: an
    unset knob may not force a write, or landing this feature would repaint every
    board already running."""
    engine, board, _ = _arm_only(tmp_path, [BoardItem(id="A", title="x", status="Todo")])
    calls = _spy_status(board)

    engine.tick()

    assert calls == []
    assert board.status_of("A") == "Todo"


def test_arm_pipeline_write_failure_does_not_write_status(tmp_path):
    """The roach motel: the latch fails, the column moves anyway.

    The card would leave the free column while the engine does NOT consider it armed
    — `pipeline is None`, so it never dispatches, and the column says an agent has
    it. Nothing would ever pick it up again. Ordering is the whole guard: the latch
    goes first, so a failed latch cannot be followed by a status write.
    """
    engine, board, _ = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Todo")], working_status="In Progress"
    )
    calls = _spy_status(board)
    board.set_pipeline = _boom

    res = engine.tick()

    assert calls == []                    # nothing claimed the card
    assert board.status_of("A") == "Todo"  # still in the free column
    assert "A" in res.skipped
    assert "A" not in res.armed


def test_arm_status_write_failure_still_arms_and_reports_no_transition(tmp_path):
    """The column is a DISPLAY of the latch, so its failure may not un-arm the item.

    Nor may it claim the transition happened: the record prints what `status_to`
    says, and a row reading `"Todo"→"In Progress"` for a write that 403'd is a lie
    in the one artifact that exists to be true.
    """
    engine, board, _ = _arm_only(
        tmp_path, [BoardItem(id="A", title="x", status="Todo")], working_status="In Progress"
    )
    emitted = []
    engine.recorder = _SpyRecorder(emitted)
    board.set_status = _boom

    res = engine.tick()

    assert board.pipeline_of("A") == "queued"   # armed: the latch landed
    assert "A" in res.armed
    armed = [e for e in emitted if e[0] == "armed"]
    assert armed and armed[0][2]["status_to"] is None   # no transition claimed
    assert "403" in res.notes                            # but the ledger says why


def test_arm_bounce_reset_failure_does_not_arm(tmp_path):
    """Reset BEFORE the latch, because the reset is write-once-or-never.

    ARM is the only place the durable rework budget is zeroed. Reset AFTER the latch
    means: latch lands, reset 403s, item is armed forever with a STALE bounce count
    and no second chance to clear it — it re-escalates on a count from an earlier
    life. Reset first, and a failure simply leaves the item unarmed for a retry.
    """
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo")],
        stages=_reject_pipeline(),
        max_rounds=3,
    )
    board.set_number = _boom

    res = engine.tick()

    assert board.pipeline_of("A") is None   # NOT armed — retried next tick
    assert "A" in res.skipped
    assert "A" not in res.armed


# === the item lock =========================================================


def test_arm_skipped_while_item_lock_held(tmp_path):
    """The ARM loop ran outside the per-item lock — a real mid-flight corruption.

    A human clears Pipeline while a stage is running. The next poll's ARM loop sees
    `pipeline is None` in the trigger column, re-arms, and zeroes Bounces UNDER the
    running stage: the rework budget resets to 0 mid-run and the cap stops meaning
    anything. `held_by_alive()` is the right predicate — `acquire()` would STEAL a
    dead lock, which is exactly what the dispatch loop wants and ARM must not do.
    """
    engine, board, runner = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo")],
        stages=_reject_pipeline(),
        max_rounds=3,
    )
    board.set_number("A", "Bounces", 2)          # a live run's rework budget
    assert Lock(engine.lockdir, "A").acquire()   # our own pid → alive → held

    res = engine.tick()

    assert board.pipeline_of("A") is None     # not armed under the running stage
    assert board.bounces_of("A") == 2         # the budget survived
    assert "A" not in res.armed
    assert runner.total_calls == 0


def test_arm_proceeds_once_the_lock_is_released(tmp_path):
    """The negative path: the lock is what blocks arming, not something else."""
    engine, board, _ = _arm_only(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo")],
        stages=_reject_pipeline(),
        max_rounds=3,
    )
    lock = Lock(engine.lockdir, "A")
    assert lock.acquire()
    engine.tick()
    assert board.pipeline_of("A") is None

    lock.release()
    res = engine.tick()

    assert board.pipeline_of("A") == "queued"
    assert "A" in res.armed


# === require_issue (row 10b) ===============================================


def test_draft_card_never_arms(tmp_path):
    """ZERO LLM spend is the property — not merely an unwritten pipeline field.

    A draft card has no backing issue, so it can carry no `Closes #N`, no PR link
    and no comment. Arming one burns every expensive stage on something that can
    never reach a PR, then fails to report anywhere. Board #32 holds 4 draft cards.
    """
    engine, board, runner = _engine(
        tmp_path,
        [BoardItem(id="A", title="draft", status="Todo", issue_number=None)],
        require_issue=True,
    )

    for _ in range(5):
        res = engine.tick()

    assert runner.total_calls == 0        # the property: nothing was spent
    assert board.pipeline_of("A") is None
    assert "A" not in res.armed
    assert "no issue behind it" in res.notes   # loud in the ledger, every tick


def test_draft_card_gate_writes_nothing_to_the_board(tmp_path):
    """Refused, not parked. A park is a WRITE that outlives the defect: convert the
    draft to a real issue and the card stays `parked` until a human clears Pipeline
    by hand. Skipping self-heals the moment the card grows an issue."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="draft", status="Todo", issue_number=None)],
        require_issue=True,
        working_status="In Progress",
    )
    calls = _spy_status(board)

    res = engine.tick()

    assert board.pipeline_of("A") is None   # not "parked"
    assert "A" not in res.parked
    assert calls == []
    assert board.status_of("A") == "Todo"


def test_card_with_an_issue_arms_under_require_issue(tmp_path):
    """The negative path: `require_issue` gates on the MISSING issue, not on being on."""
    engine, board, runner = _arm_only(
        tmp_path,
        [BoardItem(id="A", title="real", status="Todo", issue_number=114)],
        require_issue=True,
    )

    res = engine.tick()

    assert board.pipeline_of("A") == "queued"
    assert "A" in res.armed


def test_draft_card_arms_when_require_issue_is_off(tmp_path):
    """The knob defaults to False = today's engine. Landing it must refuse nothing
    that works now."""
    engine, board, _ = _arm_only(tmp_path, [BoardItem(id="A", title="draft", status="Todo")])

    res = engine.tick()

    assert board.pipeline_of("A") == "queued"
    assert "A" in res.armed


# === the self-re-arm proof =================================================


def test_engine_status_write_cannot_rearm(tmp_path):
    """The worst case the config allows: the engine writes the TRIGGER value at ARM.

    If arming were gated on the status column, this config would re-arm the card on
    every tick forever, zeroing its rework budget each time. It does not, because
    the gate is `status == trigger AND not pipeline` and the latch is already set.
    `res.armed` on the second tick is the assertion that matters — the pipeline
    field alone would look identical either way. `concurrency=0` parks the card at
    `queued` with the trigger value still in its column, tick after tick: the exact
    state the old fear describes, held still so it can be interrogated.
    """
    engine, board, _ = _arm_only(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo")],
        stages=_reject_pipeline(),
        max_rounds=3,
        working_status="Todo",   # == trigger_status: the pathological config
    )

    first = engine.tick()
    assert first.armed == ["A"]
    assert board.status_of("A") == "Todo"   # the engine wrote the TRIGGER value back
    board.set_number("A", "Bounces", 2)     # a rework budget that a re-arm would wipe

    for _ in range(4):
        later = engine.tick()

    assert later.armed == []              # never armed twice
    assert board.status_of("A") == "Todo"  # still sitting in the trigger column
    assert board.bounces_of("A") == 2     # the budget was never re-zeroed


def test_parked_card_in_the_trigger_column_never_rearms(tmp_path):
    """The same proof at the other end: park writes `parked` to the pipeline, so a
    card that never leaves the trigger column (park_status unset) is still inert."""
    bad = "A; rm -rf ~"   # fails valid_item_id → parks during ARM
    engine, board, runner = _engine(tmp_path, [BoardItem(id=bad, title="bad", status="Todo")])

    engine.tick()
    assert board.pipeline_of(bad) == "parked"
    assert board.status_of(bad) == "Todo"   # still in the trigger column

    for _ in range(4):
        res = engine.tick()

    assert res.parked == []          # parked exactly once, then inert
    assert runner.total_calls == 0


# === park_status ===========================================================


def test_park_writes_park_status(tmp_path):
    """The record's worst lie: a card with an exhausted rework budget sits in the
    TRIGGER column looking freshly dragged."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")],
        park_status="Blocked",
    )
    engine.runner.run = lambda stage, item: StageResult(ok=False, notes="boom")

    res = engine.tick()

    assert board.pipeline_of("A") == "parked"
    assert board.status_of("A") == "Blocked"
    assert "A" in res.parked


def test_park_without_park_status_leaves_the_column_alone(tmp_path):
    engine, board, _ = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")]
    )
    calls = _spy_status(board)
    engine.runner.run = lambda stage, item: StageResult(ok=False, notes="boom")

    engine.tick()

    assert board.pipeline_of("A") == "parked"
    assert calls == []
    assert board.status_of("A") == "Doing"


def test_park_pipeline_write_failure_does_not_write_park_status(tmp_path):
    """The roach motel at the park site: the column says Blocked, the engine does not.

    The pipeline write IS the park — durable and engine-owned. The column is a label
    for it. Label first and a failing latch leaves a card that reads `Blocked` to
    every human while the engine still counts it in-flight and re-dispatches it on
    the next poll. The status write is best-effort and cannot raise, so ordering is
    the ONLY thing that holds this: it is pinned here rather than trusted.
    """
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")],
        park_status="Blocked",
    )
    engine.runner.run = lambda stage, item: StageResult(ok=False, notes="boom")
    calls = _spy_status(board)
    board.set_pipeline = _boom

    res = engine.tick()

    assert calls == []                    # nothing labelled the card
    assert board.status_of("A") == "Doing"
    assert "A" not in res.parked          # and nothing claimed it parked
    assert "A" in res.skipped


def test_park_status_write_failure_still_parks(tmp_path):
    """A cosmetic write may never keep an item off its terminal state. The pipeline
    field is written FIRST, so a failed column write loses the label, never the
    park — and `res.parked` must still report it, or the ledger loses the event."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Doing", pipeline="implementing")],
        park_status="Blocked",
    )
    engine.runner.run = lambda stage, item: StageResult(ok=False, notes="boom")
    board.set_status = _boom

    res = engine.tick()

    assert board.pipeline_of("A") == "parked"
    assert "A" in res.parked
    assert "403" in res.notes


# === dispatch self-heal ====================================================


def test_dispatch_self_heals_drifted_status(tmp_path):
    """A human dragged an in-flight card back to a free column. The pipeline field
    still owns the state machine, so the card keeps running while the board says it
    is unclaimed — and someone picks up work an agent is holding."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo", pipeline="implementing")],
        working_status="In Progress",
    )

    res = engine.tick()

    assert board.status_of("A") == "In Progress"
    assert "A" in res.dispatched


def test_dispatch_self_heal_never_overwrites_human_done(tmp_path):
    """Ordering guard: Done is the human's TERMINAL signal and is read from the same
    `i.status` the heal overwrites. Heal first and the halt check reads the value the
    engine just wrote — the human's decision is gone before it is ever consulted."""
    engine, board, runner = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Done", pipeline="implementing")],
        working_status="In Progress",
        done_status="Done",
    )

    res = engine.tick()

    assert board.status_of("A") == "Done"
    assert runner.total_calls == 0
    assert "A" in res.skipped
    assert "A" not in res.dispatched


def test_dispatch_self_heal_is_best_effort(tmp_path):
    """A failing cosmetic write may not stall the pipeline forever. The heal
    advances nothing; letting it raise would hand a display write veto over every
    stage the item has left."""
    engine, board, runner = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo", pipeline="implementing")],
        working_status="In Progress",
    )
    board.set_status = _boom

    res = engine.tick()

    assert runner.total_calls == 1                  # the stage still ran
    assert board.pipeline_of("A") == "verifying"    # and still advanced
    assert "A" in res.dispatched


def test_parking_item_is_not_healed_first(tmp_path):
    """A card that is about to park must not first be labelled "an agent owns this".

    The heal exists to protect work in flight. An item heading for `parked` has none:
    healing it costs a board write only to contradict it a line later, and — because
    the park's own column write is best-effort — a failure there would strand the card
    reading `In Progress` forever, which is worse than the drift the heal repairs.
    """
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo", pipeline="verifying")],
        stages=_reject_pipeline(),
        max_rounds=3,
        working_status="In Progress",
        park_status="Blocked",
    )
    board.set_number("A", "Bounces", 1)   # a round landed; no note reached the item
    calls = _spy_status(board)

    res = engine.tick()

    assert "A" in res.parked
    assert calls == [("A", "Blocked")]   # exactly one write, and it is the true one


def test_idempotent_pr_path_is_not_healed_first(tmp_path):
    """Same rule at the gate's short-cut: the card's true column is pr_status, and
    the run is over. Flickering it through "In Progress" on every re-tick of a
    finished item is a write that says something false."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo", pipeline="verifying")],
        working_status="In Progress",
        pr_status="In Review",
    )
    board.open_pr("A")
    calls = _spy_status(board)

    res = engine.tick()

    assert "A" in res.skipped
    assert calls == [("A", "In Review")]


def test_dispatch_does_not_touch_status_without_working_status(tmp_path):
    engine, board, _ = _engine(
        tmp_path, [BoardItem(id="A", title="x", status="Whatever", pipeline="implementing")]
    )
    calls = _spy_status(board)

    engine.tick()

    assert calls == []


def test_gate_status_still_wins_over_the_heal(tmp_path):
    """The heal must not repaint the gate: the PR stage's whole point is to hand the
    human column over and STOP."""
    engine, board, _ = _engine(
        tmp_path,
        [BoardItem(id="A", title="x", status="Todo", pipeline="verifying")],
        working_status="In Progress",
        pr_status="In Review",
    )

    engine.tick()

    assert board.pipeline_of("A") == "pr-open"
    assert board.status_of("A") == "In Review"


class _SpyRecorder:
    """Records (event, item_id, fields) instead of rendering anything."""

    def __init__(self, sink):
        self.sink = sink

    def emit(self, event, item, **fields):
        self.sink.append((event, item.id, fields))
