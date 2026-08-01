"""StageRunner implementations.

FakeStageRunner is scripted + records every call — the test asserts against it.
ClaudeStageRunner is the real adapter (filled in by the build workflow): it maps
a handler ref (skill:|workflow:|agent:|cmd:) to a detached `claude -p` / argv
subprocess. Board-sourced values are passed as argv, NEVER interpolated into a
shell string.
"""
from __future__ import annotations

from .interfaces import StageResult


class FakeStageRunner:
    """Scripted runner. `fail_stage` = id of the one stage that should fail.

    Reject knob: `reject_stage` is the id of a review stage that returns a clean
    (ok=True) `verdict="reject"` for its first `reject_rounds` runs, then passes.
    `reject_to`/`annotation` ride on the StageResult. Every run records the
    `item.annotation` + `item.bounces` it saw, so a test can prove the engine fed
    the reject note (and counter) back into the producer re-run.
    """

    def __init__(
        self,
        fail_stage=None,
        tokens=100,
        board=None,
        reject_stage=None,
        reject_rounds=1,
        reject_to=None,
        annotation="changes requested",
    ):
        self.ran_stage_ids = []
        self.pr_create_calls = 0
        self.total_calls = 0
        self.fail_stage = fail_stage
        self.tokens = tokens
        self.board = board  # optional: lets the PR stage register an opened PR
        self.reject_stage = reject_stage
        self.reject_rounds = reject_rounds
        self.reject_to = reject_to
        self.annotation = annotation
        self._rejects_emitted = {}            # stage id -> count of rejects emitted
        self.seen = []                        # (stage_id, annotation, bounces) per run

    def run(self, stage, item):
        self.total_calls += 1
        self.ran_stage_ids.append(stage.id)
        self.seen.append((stage.id, getattr(item, "annotation", ""), getattr(item, "bounces", 0)))

        if stage.id == self.reject_stage:
            emitted = self._rejects_emitted.get(stage.id, 0)
            if emitted < self.reject_rounds:
                self._rejects_emitted[stage.id] = emitted + 1
                return StageResult(
                    ok=True,                  # execution healthy — verdict carries the review result
                    verdict="reject",
                    annotation=self.annotation,
                    reject_to=self.reject_to,
                    tokens=self.tokens,
                )

        is_pr = stage.id == "pr" or stage.run.startswith("cmd:gh pr")
        ok = stage.id != self.fail_stage
        if is_pr and ok:
            self.pr_create_calls += 1
            if self.board is not None:
                self.board.open_pr(item.id)
        return StageResult(ok=ok, pr_opened=is_pr and ok, tokens=self.tokens)

    # test helper: the annotation the producer (or any stage) saw on its last run
    def last_annotation_for(self, stage_id):
        anns = [a for (sid, a, _b) in self.seen if sid == stage_id]
        return anns[-1] if anns else None
