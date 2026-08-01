"""resolve_binding: a board-pilot config IS a normal workflow/projects/<slug>.yaml.

The board/repo binding may come from an explicit `board:` block OR be derived from
the standard project-registry `project:` block (org / number / issue_repo), so a
config need not duplicate the binding. Required keys fail loudly. All offline.
"""
import pytest

from engine.cli import resolve_binding


def test_explicit_board_block_wins():
    b = resolve_binding(
        {
            "board": {
                "owner": "acme",
                "project_number": 7,
                "repo": "acme/widget",
                "status_field": "Stage",
                "pipeline_field": "Engine",
                "branch_template": "auto/{project}/{item_id}",
                "repo_path": "~/src/widget",
                "criteria_dir": "skills/board-pilot/criteria",
            },
            "project": {"org": "ignored", "number": 99, "issue_repo": "ignored/x"},
        }
    )
    assert b["owner"] == "acme"
    assert b["project_number"] == 7
    assert b["repo"] == "acme/widget"
    assert b["status_field"] == "Stage"
    assert b["pipeline_field"] == "Engine"
    assert b["branch_template"] == "auto/{project}/{item_id}"
    assert b["repo_path"] == "~/src/widget"
    assert b["criteria_dir"] == "skills/board-pilot/criteria"


def test_falls_back_to_project_registry_block():
    """No board: block — derive owner/number/repo from the registry project: block,
    and fill the board-pilot-specific knobs with their defaults."""
    b = resolve_binding(
        {"project": {"tracker": "github", "org": "acme", "number": 2, "issue_repo": "acme/board-pilot-smoke"}}
    )
    assert b["owner"] == "acme"
    assert b["project_number"] == 2
    assert b["repo"] == "acme/board-pilot-smoke"
    assert b["status_field"] == "Status"
    assert b["pipeline_field"] == "Pipeline"
    assert b["branch_template"] == "bridge/{project}/{item_id}"
    assert b["repo_path"] == "acme/board-pilot-smoke"  # defaults to repo
    assert b["criteria_dir"] is None, (
        "criteria_dir has NO default, deliberately: a relative default anchors into "
        "repo_path — the repo the agent edits — letting a stage rewrite the standard "
        "it is judged against. Absent means no stage may declare criteria:, which "
        "config.validate_transparency enforces at load."
    )


def test_missing_required_keys_dies_loudly():
    with pytest.raises(SystemExit):
        resolve_binding({"pipeline": {"project": "x"}})  # no board:, no project:
