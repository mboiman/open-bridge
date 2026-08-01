"""Characterization guard for EVERY park site — written BEFORE the `_park()` refactor.

These tests pin behaviour that is CORRECT TODAY and that no other test covers.
They are not defect proofs: every one of them passed the moment it was written,
which is the point — a characterization test that fails on arrival is pinning a
bug, not a contract.

Why they exist: the park sites read like copy-paste of each other and they are
NOT. Two axes diverge silently.

There are NINE park sites. Six was the original count; capping the rewind edge (spec
row 1) added two on the crash path, and the blind-rework guard added a ninth that the
wave-3b inventory listed as a guard but never as a park site — it is one, it writes
`parked`, and it has its own accounting profile like every other.

  concurrency accounting — does this park consume one of `cfg.concurrency`
  dispatch slots for the tick?

      arm/invalid-id       does NOT consume    (parks before the dispatch loop)
      dispatch/invalid-id  does NOT consume    (parks before the runner is called)
      blind-rework         does NOT consume    (parks before the runner is called)
      reject/unresolvable  DOES consume        (`dispatched += 1`)
      reject/counter-write DOES consume        (`dispatched += 1`)
      reject/exhausted     DOES consume        (`dispatched += 1`)
      rewind/exhausted     does NOT consume    (crash path: no increment)
      rewind/counter-write does NOT consume    (crash path: no increment)
      stage-failure        does NOT consume    (falls through, no increment)

  The split is not arbitrary. The reject path increments because a REVIEW ran and
  returned a verdict — real work, so the slot is spent. The whole crash path
  (retry / rewind / park) never increments, so one card failing on every tick cannot
  starve a `concurrency: 1` board.

  attempts handling — three different dispositions:

      untouched                            (arm/invalid-id, dispatch/invalid-id,
                                            blind-rework, reject/unresolvable)
      reset(attempt_key) only              (reject/exhausted, rewind/exhausted,
                                            rewind/counter-write, stage-failure)
      reset(attempt_key) AND reset(bounce_key)   (reject/counter-write)

A refactor that folds these into one helper without preserving BOTH axes changes
concurrency semantics and durable-counter hygiene silently — the board still shows
`parked`, so nothing looks wrong. That is what these tests catch.

The concurrency probe is always the same shape: `concurrency=1`, the parking item
FIRST in board order, a healthy item SECOND. If the park consumed the slot the
healthy item does not run this tick; if it did not, it does.
"""
from engine.board import FakeBoardClient
from engine.config import EngineConfig
from engine.interfaces import BoardItem, Stage, StageResult
from engine.runner import FakeStageRunner
from engine.tick import Engine

BAD_ID = "A; rm -rf ~"  # fails valid_item_id → the two invalid-id park sites


def _plain_pipeline():
    """spec→implement→verify→pr. before-keys: queued/specced/implementing/verifying."""
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(id="verify", run="cmd:true", on_success="verifying"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def _reject_pipeline():
    """review carries the reject edge back to implement (strictly upstream)."""
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(id="review", run="cmd:true", on_success="reviewing", reject_to="implement"),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


def _rewind_pipeline():
    """verify rewinds to implement's before-key on a crash."""
    return [
        Stage(id="spec", run="cmd:true", on_success="specced"),
        Stage(id="implement", run="cmd:true", on_success="implementing"),
        Stage(
            id="verify",
            run="cmd:true",
            on_success="verifying",
            on_fail="rewind",
            rewind_to="specced",
        ),
        Stage(id="pr", run="cmd:gh pr create --draft", on_success="pr-open", gate="human"),
    ]


# === site 1: ARM, invalid item id ==========================================


def test_arm_invalid_id_park_does_not_consume_concurrency(tmp_path):
    board = FakeBoardClient(
        items=[
            BoardItem(id=BAD_ID, title="bad", status="Todo"),
            BoardItem(id="GOOD", title="good", status="Todo"),
        ]
    )
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo", concurrency=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert board.pipeline_of(BAD_ID) == "parked"
    assert BAD_ID in res.parked
    # the parked card never entered the dispatch loop, so the healthy card still ran
    assert runner.ran_stage_ids == ["spec"]
    assert "GOOD" in res.dispatched


def test_arm_invalid_id_park_leaves_attempts_untouched(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id=BAD_ID, title="bad", status="Todo")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump(f"{BAD_ID}::spec")  # a stale count from an earlier life

    engine.tick()

    assert engine.attempts.get(f"{BAD_ID}::spec") == 1  # untouched: this site never resets


# === site 2: DISPATCH, invalid item id =====================================


def test_dispatch_invalid_id_park_does_not_consume_concurrency(tmp_path):
    """Already in-flight (pipeline set) with an id that cannot reach a shell."""
    board = FakeBoardClient(
        items=[
            BoardItem(id=BAD_ID, title="bad", status="Doing", pipeline="specced"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo", concurrency=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert board.pipeline_of(BAD_ID) == "parked"
    assert BAD_ID in res.parked
    assert runner.ran_stage_ids == ["implement"]  # the healthy card kept its slot
    assert "GOOD" in res.dispatched


def test_dispatch_invalid_id_park_leaves_attempts_untouched(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id=BAD_ID, title="bad", status="Doing", pipeline="specced")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump(f"{BAD_ID}::implement")

    engine.tick()

    assert engine.attempts.get(f"{BAD_ID}::implement") == 1


# === site 3: REJECT, unresolvable target ===================================


def test_reject_unresolvable_target_park_consumes_concurrency(tmp_path):
    """The runner names a reject target that is not a stage id → park, slot spent.

    `dispatched += 1` here is deliberate: the expensive review DID run. Dropping
    the increment would let a second item's stage run in the same tick, past the
    declared concurrency ceiling.
    """
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="ghost-stage")
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "BAD" in res.parked
    assert "ghost-stage" in res.notes  # the unresolvable target is named
    assert runner.ran_stage_ids == ["review"]  # GOOD did NOT get a slot
    assert "GOOD" not in res.dispatched
    assert board.bounces_of("BAD") == 0  # parks BEFORE the counter bump


def test_reject_unresolvable_target_park_leaves_attempts_untouched(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="ghost-stage")
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::review")

    engine.tick()

    assert engine.attempts.get("BAD::review") == 1  # untouched: parks before any reset


# === site 4: REJECT, bounce-counter write failed, local backstop exhausted ==


def _break_counter(board):
    def boom(*a, **k):
        raise RuntimeError("gh failed (403): needs project scope")

    board.get_number = boom
    board.set_number = boom


def test_reject_counter_write_park_consumes_concurrency(tmp_path):
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="implement")
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    _break_counter(board)

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "BAD" in res.parked
    assert "local backstop exhausted" in res.notes
    assert runner.ran_stage_ids == ["review"]
    assert "GOOD" not in res.dispatched


def test_reject_counter_write_park_resets_both_attempt_and_bounce_keys(tmp_path):
    """The ONLY site that clears two keys. The bounce backstop key is local state
    that exists solely to bound this edge; leaving it set would make a re-armed
    item start its next life already part-way to the cap."""
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="implement")
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::review")
    _break_counter(board)

    engine.tick()

    assert engine.attempts.get("BAD::review") == 0
    assert engine.attempts.get("BAD::review::bounce-write") == 0


# === site 5: REJECT, rounds exhausted ======================================


def test_reject_rounds_exhausted_park_consumes_concurrency(tmp_path):
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="implement", reject_rounds=5)
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "rounds exhausted" in res.notes
    assert board.bounces_of("BAD") == 1  # the durable write LANDED before the cap check
    assert runner.ran_stage_ids == ["review"]
    assert "GOOD" not in res.dispatched


def test_reject_rounds_exhausted_park_resets_attempt_key_only(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board, reject_stage="review", reject_to="implement", reject_rounds=5)
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::review")

    engine.tick()

    assert engine.attempts.get("BAD::review") == 0


# === site 6: STAGE FAILURE (non-retryable) =================================


def test_park_on_failure_does_not_consume_concurrency(tmp_path):
    """Spec row 7's named invariant.

    A crashed stage parks WITHOUT `dispatched += 1`, so a second item still gets a
    slot in the same tick. That is load-bearing: with `concurrency: 1`, one card
    that parks on every tick would otherwise starve the whole board — nothing else
    would ever run while it sat there failing.
    """
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="specced"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board)
    calls = []

    def run(stage, item):
        calls.append((stage.id, item.id))
        if item.id == "BAD":
            return StageResult(ok=False, notes="boom: push rejected (ruleset)")
        return StageResult(ok=True)

    runner.run = run
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo", concurrency=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "BAD" in res.parked
    assert ("implement", "GOOD") in calls  # the failing card did NOT eat the slot
    assert "GOOD" in res.dispatched


def test_stage_failure_park_resets_attempt_key(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="specced")])
    runner = FakeStageRunner(board=board, fail_stage="implement")
    cfg = EngineConfig(stages=_plain_pipeline(), trigger_status="Todo")
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::implement")

    engine.tick()

    assert engine.attempts.get("BAD::implement") == 0


# === site 9: BLIND REWORK (bounced, no note reached the item) ==============
# Landed in wave 3b as a guard and was never inventoried as a park site. It is one:
# it writes `parked` through the same helper as the other eight, so it needs the same
# two axes pinned or a refactor can move them unnoticed.


def test_blind_rework_park_does_not_consume_concurrency(tmp_path):
    """Reads like the reject sites — it is on the reject EDGE and names its round —
    but it parks BEFORE the runner, so no review ran and no slot is spent. Folding it
    in with its reject neighbours would start charging a slot for work never done."""
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    board.set_number("BAD", "Bounces", 1)   # the round landed; no round-1 note exists

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "BAD" in res.parked
    assert runner.ran_stage_ids == ["implement"]   # the healthy card kept its slot
    assert "GOOD" in res.dispatched


def test_blind_rework_park_leaves_attempts_untouched(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board)
    cfg = EngineConfig(stages=_reject_pipeline(), trigger_status="Todo", max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::review")
    board.set_number("BAD", "Bounces", 1)

    engine.tick()

    assert engine.attempts.get("BAD::review") == 1   # untouched: parks before any reset


# === site 7: REWIND, rounds exhausted ======================================
# Added by the rewind cap (spec row 1). It reads like the reject/exhausted site
# but sits on the crash path, so its concurrency accounting is the OPPOSITE.


def test_rewind_exhausted_park_does_not_consume_concurrency(tmp_path):
    """Unlike reject/exhausted, this park does NOT spend a dispatch slot.

    It lives on the crash path, where no branch increments `dispatched`. Pinning it
    against its reject twin is the point: a refactor that folds "backward edge hits
    its cap" into one helper would silently start charging a slot here (or stop
    charging one there) and no other test would notice.
    """
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_rewind_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)

    res = engine.tick()   # BAD: verify crashes → rewind round 1 = cap → park

    assert board.pipeline_of("BAD") == "parked"
    assert "rewind rounds exhausted" in res.notes
    assert board.bounces_of("BAD") == 1
    assert ("verify" in runner.ran_stage_ids) and ("implement" in runner.ran_stage_ids)
    assert "GOOD" in res.dispatched   # the crashed card did NOT eat the slot


def test_rewind_exhausted_park_resets_attempt_key_only(tmp_path):
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_rewind_pipeline(), trigger_status="Todo", max_rounds=1)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::verify")

    engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert engine.attempts.get("BAD::verify") == 0


# === site 8: REWIND, bounce-counter write failed ===========================


def test_rewind_counter_write_park_does_not_consume_concurrency(tmp_path):
    """Fail-closed park when the durable counter cannot be written — crash-path
    accounting, so still no slot spent."""
    board = FakeBoardClient(
        items=[
            BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing"),
            BoardItem(id="GOOD", title="good", status="Doing", pipeline="specced"),
        ]
    )
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_rewind_pipeline(), trigger_status="Todo", concurrency=1, max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    _break_counter(board)

    res = engine.tick()

    assert board.pipeline_of("BAD") == "parked"
    assert "no durable terminator" in res.notes
    assert "GOOD" in res.dispatched


def test_rewind_counter_write_park_resets_attempt_key_only(tmp_path):
    """Contrast with the reject counter-write site, which clears TWO keys: this path
    never touches the local bounce backstop, because it parks instead of climbing it."""
    board = FakeBoardClient(items=[BoardItem(id="BAD", title="bad", status="Doing", pipeline="implementing")])
    runner = FakeStageRunner(board=board, fail_stage="verify")
    cfg = EngineConfig(stages=_rewind_pipeline(), trigger_status="Todo", max_rounds=3)
    engine = Engine(cfg, board, runner, state_dir=tmp_path)
    engine.attempts.bump("BAD::verify")
    _break_counter(board)

    engine.tick()

    assert engine.attempts.get("BAD::verify") == 0
    assert engine.attempts.get("BAD::verify::bounce-write") == 0   # never created here
