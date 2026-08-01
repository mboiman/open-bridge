"""Prereq 3 — validate_chain at config load.

Every backward/forward target (`on_success`, `rewind_to`, later `reject_to`) must
resolve to a real pipeline before-key (or the terminal value) at LOAD time. A typo
in a rewind/reject target otherwise produces a SILENT permanent stall at runtime
(the engine sets a pipeline value `_next_stage` can never match, and the item just
sits there) instead of a loud, named ValueError the operator can act on.
"""
import pytest

from engine.config import load_pipeline_from_dict


def _stages_with_rewind(to):
    return {
        "pipeline": {
            "trigger": {"on_status": "Todo"},
            "stages": [
                {"id": "spec", "run": "cmd:true", "on_success": "specced"},
                {"id": "implement", "run": "cmd:true", "on_success": "implementing"},
                {
                    "id": "verify",
                    "run": "cmd:true",
                    "on_success": "verifying",
                    "on_fail": {"then": "rewind", "to": to},
                },
                {"id": "pr", "run": "cmd:gh pr create --draft", "on_success": "pr-open", "gate": "human"},
            ],
        }
    }


def test_valid_rewind_target_loads():
    cfg = load_pipeline_from_dict(_stages_with_rewind("specced"))  # specced = implement's before-key
    assert [s.id for s in cfg.stages] == ["spec", "implement", "verify", "pr"]


def test_typo_rewind_target_fails_loud():
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_stages_with_rewind("implementign"))  # typo
    msg = str(exc.value)
    assert "verify" in msg          # names the offending stage
    assert "implementign" in msg    # names the bad target
