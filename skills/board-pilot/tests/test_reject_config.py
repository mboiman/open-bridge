"""V1 — the `on_reject:` config grammar parses, and a forward/typo reject target
fails LOUD at config load (never a silent runtime stall toward the PR gate)."""
from pathlib import Path

import pytest

from engine.config import load_pipeline_from_dict

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def _cfg(review_reject_to, max_rounds=2, rework_max=3):
    return {
        "pipeline": {
            "trigger": {"on_status": "Todo"},
            "rework": {"max_rounds": rework_max, "bounce_field": "Bounces"},
            "stages": [
                {"id": "spec", "run": "cmd:true", "on_success": "specced"},
                {"id": "implement", "run": "cmd:true", "on_success": "implementing"},
                {"id": "verify", "run": "cmd:true", "on_success": "verifying"},
                {
                    "id": "review",
                    "run": "cmd:true",
                    "on_success": "reviewing",
                    "on_reject": {"to": review_reject_to, "max_rounds": max_rounds, "on_exhausted": "park"},
                },
                {"id": "pr", "run": "cmd:gh pr create --draft", "on_success": "pr-open", "gate": "human"},
            ],
        }
    }


def test_on_reject_block_parses():
    cfg = load_pipeline_from_dict(_cfg("implement"))
    review = next(s for s in cfg.stages if s.id == "review")
    assert review.reject_to == "implement"
    assert review.max_rounds == 2
    assert review.on_exhausted == "park"
    assert cfg.max_rounds == 3
    assert cfg.bounce_field == "Bounces"


def test_example_config_carries_the_reject_edge():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ASSETS / "pipeline.example.yaml").read_text(encoding="utf-8"))
    cfg = load_pipeline_from_dict(data)
    review = next(s for s in cfg.stages if s.id == "review")
    assert review.reject_to == "implement"
    assert review.max_rounds == 2
    assert cfg.max_rounds == 3


def test_forward_reject_target_fails_loud():
    # rejecting to a DOWNSTREAM stage (toward the PR gate) must be refused at load
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_cfg("pr"))
    assert "review" in str(exc.value) and "pr" in str(exc.value)


def test_unknown_reject_target_fails_loud():
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_cfg("implemnt"))  # typo
    assert "implemnt" in str(exc.value)


def test_reject_without_resolvable_max_rounds_fails_loud():
    # on_reject declared but neither per-edge nor rework max_rounds → loud refusal
    bad = _cfg("implement", max_rounds=None, rework_max=None)
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(bad)
    assert "max_rounds" in str(exc.value)
