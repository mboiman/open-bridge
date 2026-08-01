"""Step 6 — the transparency config block parses, or is refused BY NAME.

`load_pipeline_from_dict` builds EngineConfig from a FIXED keyword set, so a knob
it does not read is not a no-op with a warning — it is invisible. Silently
ignoring `require_issue` arms draft cards and burns an LLM run on an item that can
never carry a `Closes #N`, a PR link or a comment. Every knob below is therefore
either parsed or refused with a ValueError that names the offending value, the way
validate_chain names a stage AND its target.
"""
import pytest

from engine.config import load_pipeline_from_dict


def _base(**pipeline_extra):
    """Today's config: a valid chain and nothing transparency-related."""
    pl = {
        "trigger": {"on_status": "Todo"},
        "rework": {"max_rounds": 3, "bounce_field": "Bounces"},
        "stages": [
            {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
            {"id": "verify", "run": "cmd:true", "on_success": "reviewing"},
            {"id": "pr", "run": "cmd:gh pr create --draft", "on_success": "pr-open", "gate": "human"},
        ],
    }
    pl.update(pipeline_extra)
    return {"pipeline": pl}


# --- backward compatibility -------------------------------------------------


def test_transparency_block_absent_yields_todays_config():
    """A config with no transparency block must behave exactly as it does today.

    Every new knob has to be inert by default, or landing this step silently
    changes the behaviour of every pipeline already running.
    """
    cfg = load_pipeline_from_dict(_base())

    # today's fields, today's values
    assert cfg.trigger_status == "Todo"
    assert cfg.pr_status == "In Review"
    assert cfg.done_status == "Done"
    assert cfg.concurrency == 1
    assert cfg.token_ceiling is None
    assert cfg.bounce_field == "Bounces"
    assert cfg.max_rounds == 3

    # the new knobs, all inert
    assert cfg.working_status is None, "no working_status configured → the engine must not write one at ARM"
    assert cfg.park_status is None, "no park_status configured → the engine must not write one at park"
    assert cfg.require_issue is False, "require_issue must default OFF — it is opt-in, not a surprise refusal"
    assert cfg.record.enabled is False, "the record layer must not switch itself on"
    assert cfg.evidence.dir is None
    assert all(s.criteria is None for s in cfg.stages)
    assert all(s.evidence is False for s in cfg.stages)


def test_require_deterministic_defaults_closed():
    """The one new default that is NOT off: an unconfigured evidence block still
    refuses an agent stage as an evidence source. Safe because no config today
    declares per-stage `evidence:` at all, so nothing existing changes."""
    cfg = load_pipeline_from_dict(_base())
    assert cfg.evidence.require_deterministic is True


# --- the paste-ready block from the spec (§1.3) must load -------------------


SPEC_YAML = """
board:
  status_field: Workflow
  pipeline_field: Pipeline
  repo: bks-lab/open-bridge
  repo_path: "~/Developer/your-org/open-bridge"
  stages_dir: "skills/board-pilot/stages"
  criteria_dir: "skills/board-pilot/criteria"
  branch_template: "bridge/{project}/{item_id}"

pipeline:
  project: open-bridge-dev
  trigger:
    on_status: "Ready for Development"
  working_status: "In Progress"
  park_status: "Blocked"
  pr_status: "In Review"
  done_status: "Done"
  concurrency: 1
  require_issue: true

  rework:
    max_rounds: 3
    bounce_field: Bounces

  budget:
    max_tokens: null

  record:
    enabled: true
    events: [armed, stage, reject, park, gate]
    sticky_marker: "board-pilot:run"
    templates_dir: "skills/board-pilot/templates"
    max_body_chars: 60000
    scan: redact
  evidence:
    dir: "{state_dir}/evidence/{item_id}"
    require_deterministic: true

  stages:
    - name: spec
      run: "cmd:bash spec.sh"
      criteria: "spec.md"
      on_success: implementing
      on_fail: { then: park }

    - name: implement
      run: "cmd:bash implement.sh"
      criteria: "implement.md"
      on_success: verifying
      on_fail: { retry: 1, then: park }

    - name: verify
      run: "cmd:bash verify.sh"
      evidence: true
      on_success: reviewing
      on_reject: { to: implement }
      on_fail: { retry: 1, then: park }

    - name: review
      run: "cmd:bash review.sh"
      criteria: "review.md"
      on_success: pr-ready
      on_reject: { to: implement, on_exhausted: park }

    - name: pr
      run: "cmd:bash pr.sh"
      on_success: pr-open
      gate: human
"""


def test_spec_pipeline_block_loads():
    """The block the spec ships as paste-ready must parse — including through
    validate_chain, whose reject edges both resolve to `implement`."""
    yaml = pytest.importorskip("yaml")
    cfg = load_pipeline_from_dict(yaml.safe_load(SPEC_YAML))

    assert [s.id for s in cfg.stages] == ["spec", "implement", "verify", "review", "pr"]
    assert cfg.trigger_status == "Ready for Development"
    assert cfg.working_status == "In Progress"
    assert cfg.park_status == "Blocked"
    assert cfg.require_issue is True
    assert cfg.token_ceiling is None, "budget.max_tokens: null is the honest value — tokens are not metered"

    assert cfg.record.enabled is True
    assert cfg.record.events == ("armed", "stage", "reject", "park", "gate")
    assert cfg.record.sticky_marker == "board-pilot:run"
    assert cfg.record.templates_dir == "skills/board-pilot/templates"
    assert cfg.record.max_body_chars == 60000
    assert cfg.record.scan == "redact"

    assert cfg.evidence.dir == "{state_dir}/evidence/{item_id}"
    assert cfg.evidence.require_deterministic is True

    by_id = {s.id: s for s in cfg.stages}
    assert by_id["spec"].criteria == "spec.md"
    assert by_id["implement"].criteria == "implement.md"
    assert by_id["review"].criteria == "review.md"
    assert by_id["verify"].criteria is None
    assert by_id["verify"].evidence is True
    assert by_id["review"].evidence is False


# --- fail loud, never silent ------------------------------------------------


def test_unknown_record_mode_raises_naming_it():
    """A typo'd scan mode must not fall back to a default. `scan: reduct` reads as
    "redaction is on" and would post unscanned agent text to a public repo."""
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_base(record={"enabled": True, "scan": "reduct"}))
    msg = str(exc.value)
    assert "reduct" in msg, "the refusal must name the offending value"
    assert "redact" in msg and "off" in msg, "the refusal must name the valid set"
    assert "scan" in msg


def test_unknown_record_event_raises_naming_it():
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_base(record={"enabled": True, "events": ["armed", "staged"]}))
    msg = str(exc.value)
    assert "staged" in msg
    assert "stage" in msg and "gate" in msg, "the refusal must name the valid set"


def test_record_scan_off_is_a_valid_mode():
    cfg = load_pipeline_from_dict(_base(record={"enabled": True, "scan": "off"}))
    assert cfg.record.scan == "off"


def test_unknown_record_key_raises_naming_it():
    """The whole point of this step: a knob the loader does not read is invisible.
    An unknown key inside `record:` is a typo'd knob, and it must not be swallowed."""
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_base(record={"enabled": True, "sticky_marked": "x"}))
    msg = str(exc.value)
    assert "sticky_marked" in msg
    assert "sticky_marker" in msg, "the refusal must name the valid set so the typo is obvious"


def test_unknown_evidence_key_raises_naming_it():
    """`require_deterministic` misspelled is the worst silent knob of the set: it
    reads as "an LLM can never be an evidence source" while being ignored."""
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(_base(evidence={"require_determinstic": False}))
    msg = str(exc.value)
    assert "require_determinstic" in msg
    assert "require_deterministic" in msg


# --- evidence.require_deterministic, enforced AT LOAD -----------------------


def test_agent_stage_cannot_be_evidence_source():
    """An LLM stage may never be an evidence source: it would be reporting on
    itself, and the dossier labels that output as machine-captured."""
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(
            _base(
                evidence={"require_deterministic": True},
                stages=[
                    {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                    {"id": "verify", "run": "agent:code-reviewer", "evidence": True, "on_success": "reviewing"},
                    {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
                ],
            )
        )
    msg = str(exc.value)
    assert "verify" in msg, "the refusal must name the offending stage"
    assert "agent:code-reviewer" in msg, "the refusal must name the run: that disqualified it"
    assert "cmd:" in msg


def test_deterministic_stage_may_be_evidence_source():
    cfg = load_pipeline_from_dict(
        _base(
            stages=[
                {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                {"id": "verify", "run": "cmd:pytest -q", "evidence": True, "on_success": "reviewing"},
                {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
            ]
        )
    )
    assert next(s for s in cfg.stages if s.id == "verify").evidence is True


def test_require_deterministic_false_allows_an_agent_evidence_source():
    """The knob has to actually be a knob — otherwise it is decoration."""
    cfg = load_pipeline_from_dict(
        _base(
            evidence={"require_deterministic": False},
            stages=[
                {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                {"id": "verify", "run": "agent:code-reviewer", "evidence": True, "on_success": "reviewing"},
                {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
            ],
        )
    )
    assert next(s for s in cfg.stages if s.id == "verify").evidence is True


# --- criteria ---------------------------------------------------------------


def test_criteria_without_criteria_dir_raises_naming_the_stage():
    """`criteria:` is a filename relative to board.criteria_dir. Without that base
    the path resolves to nothing, and the record would cite a criteria ref the
    engine never read — the exact silent-degradation this step exists to prevent.
    This is the only criteria fact answerable from the config alone; whether the
    FILE exists is a fact about the filesystem and belongs at engine preflight.
    """
    with pytest.raises(ValueError) as exc:
        load_pipeline_from_dict(
            _base(
                stages=[
                    {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                    {"id": "review", "run": "cmd:true", "criteria": "review.md", "on_success": "reviewing"},
                    {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
                ]
            )
        )
    msg = str(exc.value)
    assert "review" in msg, "the refusal must name the offending stage"
    assert "criteria_dir" in msg, "the refusal must name the missing knob"


def test_criteria_parses_when_criteria_dir_is_configured():
    cfg = load_pipeline_from_dict(
        {
            "board": {"criteria_dir": "skills/board-pilot/criteria"},
            "pipeline": _base(
                stages=[
                    {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                    {"id": "review", "run": "cmd:true", "criteria": "review.md", "on_success": "reviewing"},
                    {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
                ]
            )["pipeline"],
        }
    )
    assert next(s for s in cfg.stages if s.id == "review").criteria == "review.md"


def test_nonexistent_criteria_file_still_loads():
    """Deliberate: the file's existence is NOT checked here.

    The spec's own build order proves why — the project YAML lands at step 14 and
    the criteria files at step 15, so a load-time stat would make the shipped
    config unloadable in between. Existence is environmental, like a board option,
    and belongs with preflight_reject_field at engine init (still before any
    spend), not in a loader that legitimately runs where the target repo does not.
    """
    cfg = load_pipeline_from_dict(
        {
            "board": {"criteria_dir": "/nonexistent/criteria/dir"},
            "pipeline": _base(
                stages=[
                    {"id": "implement", "run": "cmd:true", "on_success": "verifying"},
                    {"id": "review", "run": "cmd:true", "criteria": "no-such-file.md", "on_success": "reviewing"},
                    {"id": "pr", "run": "cmd:true", "on_success": "pr-open", "gate": "human"},
                ]
            )["pipeline"],
        }
    )
    assert next(s for s in cfg.stages if s.id == "review").criteria == "no-such-file.md"


# --- the status knobs -------------------------------------------------------


def test_working_and_park_status_parse():
    cfg = load_pipeline_from_dict(_base(working_status="In Progress", park_status="Blocked"))
    assert cfg.working_status == "In Progress"
    assert cfg.park_status == "Blocked"


def test_require_issue_parses():
    assert load_pipeline_from_dict(_base(require_issue=True)).require_issue is True
