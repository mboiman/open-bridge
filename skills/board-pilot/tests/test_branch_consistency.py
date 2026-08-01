"""Regression guard: the board adapter and the stage runner MUST render the SAME
per-item head branch.

They are constructed separately in cli.py (one needs the board binding, the other
the repo path), so a drift between them — e.g. the board keying the head off the
project NUMBER while the runner keys it off the project SLUG — silently breaks the
`pr_exists` idempotency check: the guard looks at the wrong head, finds no PR, and
a re-tick opens a DUPLICATE PR. This pins the invariant. Neither constructor
touches the network, so it runs offline like the rest of the suite.
"""
from engine.claude_runner import ClaudeStageRunner
from engine.gh_board import GhBoardClient
from engine.interfaces import BoardItem


def _pair(template: str, slug: str):
    runner = ClaudeStageRunner(repo="x", branch_template=template, project=slug)
    board = GhBoardClient(
        project_number=7,
        owner="o",
        status_field="Status",
        pipeline_field="Pipeline",
        repo="o/r",
        branch_template=template,
        project=slug,
    )
    return runner, board


def test_board_and_runner_agree_on_head_branch():
    runner, board = _pair("bridge/{project}/{item_id}", "demo-proj")
    item = BoardItem(id="PVTI_abc123", title="t", status="Todo")
    runner_branch = runner._item_env(item)["BRANCH"]
    board_head = board.branch_for(item.id)
    assert runner_branch == board_head == "bridge/demo-proj/PVTI_abc123"


def test_agreement_holds_for_a_custom_template():
    runner, board = _pair("auto/{project}-{item_id}", "acme")
    item = BoardItem(id="42", title="t", status="Todo")
    assert runner._item_env(item)["BRANCH"] == board.branch_for(item.id) == "auto/acme-42"


def test_runner_expands_tilde_in_repo_path():
    """subprocess cwd does not tilde-expand; the runner must, or the stage runs in a
    literal '~' dir. (repo_path like ~/Developer/... is the normal smoke config.)"""
    import os

    runner = ClaudeStageRunner(repo="~/Developer/x", branch_template="b/{item_id}", project="p")
    assert runner.repo == os.path.expanduser("~/Developer/x")
    assert "~" not in runner.repo
