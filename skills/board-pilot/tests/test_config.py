"""Config loader — proves the real per-project YAML parses into a valid
EngineConfig (the shipped example), and that both the `uses:` and `name:` stage
keys are accepted (the example uses `uses:`, a downstream wiring uses `name:`)."""
from pathlib import Path

import pytest

from engine.config import load_pipeline_from_dict

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def test_example_config_parses_into_valid_engineconfig():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ASSETS / "pipeline.example.yaml").read_text(encoding="utf-8"))
    cfg = load_pipeline_from_dict(data)
    assert cfg.stages, "no stages parsed from the example config"
    assert all(s.id for s in cfg.stages), "a stage parsed with an empty id"
    assert all(s.on_success for s in cfg.stages), "a stage has no on_success target"
    assert cfg.trigger_status == "Todo"
    pr = cfg.stages[-1]
    assert pr.gate == "human", "the final (PR) stage must be human-gated"


def test_loader_accepts_both_uses_and_name_keys():
    cfg = load_pipeline_from_dict(
        {
            "pipeline": {
                "trigger": {"on_status": "Todo"},
                "stages": [
                    {"name": "spec", "run": "cmd:true", "on_success": "implementing"},
                    {"uses": "pr", "run": "cmd:gh pr create --draft", "on_success": "pr-open", "gate": "human"},
                ],
            }
        }
    )
    assert [s.id for s in cfg.stages] == ["spec", "pr"]
    assert cfg.stages[-1].gate == "human"
