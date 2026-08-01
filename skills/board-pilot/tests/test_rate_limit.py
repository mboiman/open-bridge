"""Rate-limit hardening — 736 overnight tick crashes, three causes, all pinned here.

The board carried 6 items, yet every 60s tick (a) requested a 1000-row page from
`gh project item-list` (GraphQL point cost scales with the REQUESTED page size,
not the returned rows), (b) read back reject-note comments for items that had
never bounced, and (c) let a rate-limited `gh project field-list` at Engine
preflight propagate as an uncaught GhCliError — traceback to err.log, launchd
relaunch, straight back into the same rate limit.

Offline: every `gh` call is monkeypatched (recorded argv), same as
test_preflight_options.py / test_gh_board_reject.py.
"""
import json

import pytest

from engine import cli
from engine.gh_board import GhBoardClient, GhCliError

yaml = pytest.importorskip("yaml")


def _client(**kw):
    return GhBoardClient(
        project_number=7,
        owner="o",
        status_field="Status",
        pipeline_field="Pipeline",
        repo="o/r",
        **kw,
    )


def _row(item_id, number=None, bounces=None):
    row = {"id": item_id, "title": "t", "status": "Todo", "content": {}}
    if number is not None:
        row["content"]["number"] = number
    if bounces is not None:
        row["bounces"] = bounces
    return row


# -- (a) page-size cost: request a bounded page, refuse a full one -----------
def test_item_list_requests_100_rows_not_1000(monkeypatch):
    """The GraphQL point cost is charged for the REQUESTED page size. -L 1000 on
    a 6-item board paid 10x the necessary cost on every 60s tick."""
    calls = []

    def _record(argv):
        calls.append(argv)
        return {"items": [_row("PVTI_a")]}

    c = _client()
    monkeypatch.setattr(c, "_run_json", _record)
    c.fetch_items()
    (argv,) = [a for a in calls if a[:3] == ["gh", "project", "item-list"]]
    assert argv[argv.index("-L") + 1] == "100"


def test_item_list_full_page_fails_loud(monkeypatch):
    """Returned row count == the limit means the board MAY be truncated — items
    beyond the page would silently never arm, never advance, never park. That
    must be the DEDICATED BoardTruncated raise (which the engine's fetch guard
    lets propagate — this is a permanent condition, not a transient skip),
    never a quietly processed partial board."""
    from engine.gh_board import BoardTruncated

    c = _client()
    monkeypatch.setattr(
        c, "_run_json", lambda argv: {"items": [_row(f"PVTI_{n}") for n in range(100)]}
    )
    with pytest.raises(BoardTruncated) as ei:
        c.fetch_items()
    assert "100" in str(ei.value)  # actionable: names the limit that was hit


def test_item_list_under_limit_passes(monkeypatch):
    """Ratchet against over-correcting: a normal small board must stay a no-raise."""
    c = _client()
    monkeypatch.setattr(
        c, "_run_json", lambda argv: {"items": [_row(f"PVTI_{n}") for n in range(6)]}
    )
    assert len(c.fetch_items()) == 6


# -- (b) comment read-back gated on bounces > 0 ------------------------------
def test_bounces_zero_never_enters_the_comment_read_back(monkeypatch):
    """bounces == 0 means no reject round has ever landed, so there is no note
    to read back. The Number field already rode in on the item-list row, so the
    gate is free — and it must live at the CALL SITE in fetch_items, not hang on
    the helper's internal round guard (the helper's argv was already quiet; the
    gate is what keeps it structurally out of the per-item hot path)."""
    calls = []

    def _record(argv):
        calls.append(argv)
        return {"items": [_row("PVTI_a", number=5, bounces=0)]}

    c = _client(bounce_field="Bounces", reject_edge=True)
    monkeypatch.setattr(c, "_run_json", _record)

    def _never(*a, **k):
        raise AssertionError("read-back entered with bounces == 0")

    monkeypatch.setattr(c, "_latest_reject_note", _never)
    items = c.fetch_items()
    assert items[0].annotation == ""
    assert not any(a[:3] == ["gh", "issue", "view"] for a in calls)


def test_bounces_positive_still_fetches_the_note(monkeypatch):
    """Ratchet against over-gating: a bounced item's note is the producer's ONLY
    rework feedback — losing it trips the blind-rework park."""
    calls = []

    def _record(argv):
        calls.append(argv)
        if argv[:3] == ["gh", "project", "item-list"]:
            return {"items": [_row("PVTI_a", number=5, bounces=2)]}
        if argv[:3] == ["gh", "issue", "view"]:
            return {
                "comments": [
                    {
                        "viewerDidAuthor": True,
                        "body": "<!-- board-pilot:reject round=2 -->\nadd the empty-case test",
                    }
                ]
            }
        raise AssertionError(f"unexpected gh call: {argv}")

    c = _client(bounce_field="Bounces", reject_edge=True)
    monkeypatch.setattr(c, "_run_json", _record)
    items = c.fetch_items()
    assert items[0].annotation == "add the empty-case test"
    assert any(a[:3] == ["gh", "issue", "view"] for a in calls)


# -- field-list: one fetch per client instance (= per tick process) ----------
def test_field_list_fetched_once_per_instance(monkeypatch):
    """Two preflights + option resolution each resolve fields. Per-FIELD caching
    still paid one full field-list read per distinct field name; the list is one
    payload, so one process needs exactly one fetch of it."""
    calls = []

    def _record(argv):
        calls.append(argv)
        return {
            "fields": [
                {"id": "F_P", "name": "Pipeline", "options": [{"name": "queued", "id": "O_q"}]},
                {"id": "F_S", "name": "Status", "options": [{"name": "Todo", "id": "O_t"}]},
                {"id": "F_B", "name": "Bounces"},
            ]
        }

    c = _client(bounce_field="Bounces", reject_edge=True)
    monkeypatch.setattr(c, "_run_json", _record)
    c.preflight_reject_field()
    c.preflight_options(["queued"], ["Todo"])
    c._field("Pipeline")
    field_lists = [a for a in calls if a[:3] == ["gh", "project", "field-list"]]
    assert len(field_lists) == 1, f"field-list fetched {len(field_lists)}x for one instance"


# -- (c) preflight crash: transient errors skip the tick, permanent ones raise
# CLI-level, following the test_cli_wiring.py pattern: cli imports the adapters
# inside run(), so the module attributes are the real seam.
_PROJECT_YAML = {
    "board": {"owner": "acme", "project_number": 1, "repo": "acme/widget"},
    "pipeline": {
        "project": "example",
        "trigger": {"on_status": "Todo"},
        "stages": [
            {"uses": "verify", "run": "cmd:make verify", "on_success": "verified"},
            {
                "uses": "pr",
                "run": "cmd:gh pr create --draft",
                "on_success": "pr-open",
                "gate": "human",
            },
        ],
    },
}


class _RunnerStub:
    def __init__(self, *args, **kwargs):
        pass


class _BoardStub:
    def __init__(self, *args, **kwargs):
        pass


def _drive_with_engine_raising(tmp_path, monkeypatch, exc):
    """Run one real cli.run() whose Engine construction (= the preflight site)
    raises `exc`. Returns the exit code; the raise, if any, propagates."""
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump(_PROJECT_YAML), encoding="utf-8")

    class _EngineBoom:
        def __init__(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr("engine.claude_runner.ClaudeStageRunner", _RunnerStub)
    monkeypatch.setattr("engine.gh_board.GhBoardClient", _BoardStub)
    monkeypatch.setattr("engine.tick.Engine", _EngineBoom)
    return cli.run(["--project", str(project), "--state-dir", str(tmp_path / "state"), "--once"])


def test_rate_limited_preflight_skips_the_tick_quietly(tmp_path, monkeypatch, capsys):
    """THE overnight defect: a rate-limited field-list at preflight crashed the
    process 736 times. The API's own backpressure must become one quiet ledger
    line + exit 0 — launchd's timer IS the retry, no relaunch storm needed."""
    err = GhCliError(
        ["gh", "project", "field-list", "1"],
        1,
        "GraphQL: API rate limit exceeded for installation ID 143530724",
    )
    rc = _drive_with_engine_raising(tmp_path, monkeypatch, err)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert "rate limit" in summary["notes"].lower()
    assert summary["dispatched"] == [] and summary["parked"] == []


def test_http_5xx_preflight_skips_the_tick_quietly(tmp_path, monkeypatch, capsys):
    """A 5xx is GitHub's failure, not the operator's — same treatment as the
    rate limit: the next tick retries against a recovered API."""
    err = GhCliError(["gh", "project", "field-list", "1"], 1, "HTTP 502: Server Error")
    rc = _drive_with_engine_raising(tmp_path, monkeypatch, err)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["notes"]  # the skip is noted in the ledger, never silent


def test_missing_field_preflight_still_raises(tmp_path, monkeypatch):
    """Fail-closed stays: a missing field/option is a PERMANENT config error —
    every future tick fails identically, so a quiet skip would hide a board that
    can never work behind a healthy-looking ledger."""
    err = RuntimeError(
        "board-pilot reject edge requires a Number field 'Bounces' on project #1 (acme), "
        "but it does not exist on the board."
    )
    with pytest.raises(RuntimeError, match="Bounces"):
        _drive_with_engine_raising(tmp_path, monkeypatch, err)


def test_permanent_gh_error_at_preflight_still_raises(tmp_path, monkeypatch):
    """A GhCliError is not transient per se: a 403 (bad token scope) never heals
    by waiting, so it must stay loud — only rate limits / 5xx earn the skip."""
    err = GhCliError(
        ["gh", "project", "field-list", "1"],
        1,
        "HTTP 403: Resource not accessible by integration",
    )
    with pytest.raises(GhCliError):
        _drive_with_engine_raising(tmp_path, monkeypatch, err)


# -- (d) the tick fetch guard: transient skips quietly, permanent raises -----
# The Engine-level twin of the cli.py preflight seam. A board fetch failure that
# is the API's fault (rate limit, 5xx, network down) skips the tick with a quiet
# ledger note; one that is PERMANENT (truncated board, bad auth/scope, unknown
# shapes) fails identically on every future tick, so a quiet exit-0 skip would be
# an invisible forever-wedge — no arm, no advance, launchd sees a healthy unit.


def _engine_over_gh(tmp_path, monkeypatch, item_rows):
    """A real Engine over a real GhBoardClient whose `gh` I/O is monkeypatched.

    The field-list payload carries every option the Engine preflights (writable
    pipeline values + pr_status), so construction succeeds and the test reaches
    tick()'s fetch guard — the seam under test."""
    from engine.config import EngineConfig
    from engine.interfaces import Stage
    from engine.runner import FakeStageRunner
    from engine.tick import Engine

    c = _client()

    def _fake(argv):
        if argv[:3] == ["gh", "project", "field-list"]:
            return {
                "fields": [
                    {
                        "id": "F_P",
                        "name": "Pipeline",
                        "options": [
                            {"name": n, "id": f"O_{n}"}
                            for n in ("queued", "verified", "pr-open", "parked")
                        ],
                    },
                    {
                        "id": "F_S",
                        "name": "Status",
                        "options": [
                            {"name": "Todo", "id": "O_todo"},
                            {"name": "In Review", "id": "O_rev"},
                        ],
                    },
                ]
            }
        if argv[:3] == ["gh", "project", "item-list"]:
            return {"items": list(item_rows)}
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr(c, "_run_json", _fake)
    stages = [
        Stage(id="verify", run="cmd:true", on_success="verified"),
        Stage(id="pr", run="cmd:true", on_success="pr-open", gate="human"),
    ]
    cfg = EngineConfig(stages=stages, trigger_status="Todo")
    engine = Engine(cfg, c, FakeStageRunner(board=c), state_dir=tmp_path / "state")
    return engine, c


def test_board_at_limit_raises_out_of_tick(tmp_path, monkeypatch):
    """THE wedge: a board at exactly the page limit is a PERMANENT condition —
    every future fetch fails identically. tick() must let BoardTruncated
    propagate (traceback, non-zero exit, human sees it), never swallow it into
    the quiet 'board fetch failed' skip note that only the stdout ledger sees."""
    from engine.gh_board import BoardTruncated

    engine, _ = _engine_over_gh(
        tmp_path, monkeypatch, [_row(f"PVTI_{n}") for n in range(100)]
    )
    with pytest.raises(BoardTruncated):
        engine.tick()


def test_transient_fetch_error_still_skips_the_tick_quietly(tmp_path, monkeypatch):
    """Ratchet against over-correcting: the API's own backpressure at fetch time
    keeps the quiet-note treatment — the poll timer is the retry, and a raise
    here would relaunch-storm straight back into the same rate limit."""
    engine, board = _engine_over_gh(tmp_path, monkeypatch, [_row("PVTI_a")])

    def _limited(argv):
        raise GhCliError(argv, 1, "GraphQL: API rate limit exceeded for installation ID 1")

    monkeypatch.setattr(board, "_run_json", _limited)
    res = engine.tick()  # must NOT raise
    assert "board fetch failed" in res.notes
    assert res.dispatched == [] and res.parked == [] and res.armed == []


def test_permanent_gh_error_at_fetch_raises_out_of_tick(tmp_path, monkeypatch):
    """A non-transient GhCliError (403 bad scope) never heals by waiting: the
    fetch guard must re-raise it, same rule as the cli.py preflight seam."""
    engine, board = _engine_over_gh(tmp_path, monkeypatch, [_row("PVTI_a")])

    def _forbidden(argv):
        raise GhCliError(argv, 1, "HTTP 403: Resource not accessible by integration")

    monkeypatch.setattr(board, "_run_json", _forbidden)
    with pytest.raises(GhCliError):
        engine.tick()


# -- network-down stderr shapes: transient, not a crash-loop at preflight ----
# An offline box must not relaunch-storm like the 736-crash night. The list is
# deliberately tight: unknown shapes must STAY non-transient (fail-closed).


@pytest.mark.parametrize(
    "stderr",
    [
        "error connecting to api.github.com",
        "dial tcp 140.82.121.6:443: connect: network is unreachable",
        'Get "https://api.github.com/graphql": dial tcp: i/o timeout',
        "Could not resolve host: api.github.com",
        "dial tcp: lookup api.github.com: no such host",
    ],
)
def test_network_down_stderr_shapes_classify_transient(stderr):
    assert GhCliError(["gh", "api", "graphql"], 1, stderr).is_transient()


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 403: Resource not accessible by integration",
        "HTTP 404: Not Found (https://api.github.com/graphql)",
        "unknown flag: --frobnicate",
        "GraphQL: Field 'frobnicate' doesn't exist on type 'Query'",
        "",
    ],
)
def test_unknown_stderr_shapes_stay_non_transient(stderr):
    """Fail-closed default: an unmatched shape is treated as permanent and stays
    loud — a widened regex that classifies garbage as transient would hide real
    defects behind quiet skip notes forever."""
    assert not GhCliError(["gh", "api", "graphql"], 1, stderr).is_transient()
