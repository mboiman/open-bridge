"""V1 — the comment I/O port: `list_comments` + `edit_comment` on both board clients.

record.py keeps ONE sticky comment per run and edits it in place, so *which*
comment gets edited is this port's entire risk surface. The engine authors TWO
comment streams on the same issue, both as itself:

  * the sticky run record — edited every event, read back for nothing;
  * the round-scoped reject note — discrete, and fed straight back to an
    autonomous code writer as its own feedback.

"the last comment" / `gh issue comment --edit-last` is therefore not a shortcut
but a defect: after a reject, the newest self-authored comment IS the note, and
editing it would overwrite the producer's instructions with the record. The damage
is silent — an edit never notifies anyone, so nothing announces that the note the
next tick reads back is now a run table.

Offline: every `gh` call is monkeypatched, so these assert argv shape and parsing
and never touch the network. The wire contracts the argv encodes were verified
live against gh 2.96.0 + the GitHub GraphQL schema (see per-test docstrings).
"""
import inspect
from datetime import datetime

import pytest

import engine.gh_board as ghmod
from engine.board import FakeBoardClient
from engine.config import RecordConfig
from engine.gh_board import GhBoardClient
from engine.interfaces import BoardItem, Stage, StageResult, parse_reject, reject_comment
from engine.record import Recorder


def _client(**kw):
    return GhBoardClient(
        project_number=7,
        owner="o",
        status_field="Status",
        pipeline_field="Pipeline",
        repo="o/r",
        **kw,
    )


def _board(item_id="A", issue_number=114):
    return FakeBoardClient(
        items=[
            BoardItem(
                id=item_id,
                title="Nothing scans agent-authored text",
                status="Ready for Development",
                issue_number=issue_number,
            )
        ]
    )


def _recorder(sink):
    return Recorder(
        sink,
        RecordConfig(enabled=True),
        status_field="Workflow",
        now=lambda: datetime(2026, 7, 15, 14, 2, 11).astimezone(),
    )


def _stage(sid="verify", run="cmd:bash verify.sh"):
    return Stage(id=sid, run=run, on_success="reviewing")


def _rows(body):
    """The data rows of the record table — the lines the run actually appended."""
    return [ln for ln in body.splitlines() if ln.startswith("| 20")]


# 1) THE ONE THAT MATTERS: the edit must find the sticky, not the newest ------
def test_edit_targets_the_sticky_not_the_reject_note():
    """An issue holding BOTH streams, both authored by this engine, with the reject
    note LAST — the arrangement in which "edit the newest self-authored comment"
    destroys the round-scoped note instead of the record.

    The note is asserted byte-identical rather than merely present: it is read back
    through a byte-0-anchored parser and handed to an autonomous code writer, so any
    edit to it — even one that preserved the marker — changes what the producer is
    told it must fix.
    """
    board = _board()
    rec = _recorder(board)
    item = board.fetch_items()[0]

    rec.emit("armed", item, to="queued", status_to="In Progress")

    # the note lands AFTER the sticky: from here on, "newest self-authored" is the
    # note, and the sticky is only findable by its marker.
    note_body = reject_comment(1, "please add the empty-case test")
    board.comment(item.id, note_body)

    rec.emit("stage", item, stage=_stage(), result=StageResult(ok=True), to="reviewing", outcome="pass")

    comments = board.list_comments(item.id)
    assert len(comments) == 2, "the sticky was edited in place — nothing new is posted"

    sticky, note = comments[0], comments[1]
    assert note["body"] == note_body                  # byte-identical: never touched
    assert parse_reject(note["body"]) == (1, "please add the empty-case test")
    assert sticky["body"].startswith("<!-- board-pilot:run item=A -->")
    assert len(_rows(sticky["body"])) == 2            # armed + stage, both in the sticky

    # and the note still WORKS, not just survives byte-wise: the producer's read-back
    # for round 1 must still return it.
    board.set_number(item.id, "Bounces", 1)
    assert board.fetch_items()[0].annotation == "please add the empty-case test"


def test_edit_targets_the_sticky_even_when_the_note_quotes_the_record_table():
    """The same property, in the ONE arrangement where marker-targeting is the only
    thing that saves the note.

    `_existing_rows` refuses to edit a body with no table separator, which incidentally
    rescues the note in the common case — so a "newest self-authored" finder is caught
    there by a guard that is defending the RECORD's history, not the note. That rescue
    evaporates the moment a note contains the record's exact separator line, which is
    reachable: the record quotes the note (record.py says so), and an annotation
    quoting the record back is the mirror image of the same habit.

    Measured against a `--edit-last` finder with this body: the note stops being
    byte-identical, stops parsing as round 1, and the read-back hands the producer an
    EMPTY annotation — i.e. the rework runs with its instructions deleted. Only the
    byte-0 marker filter prevents it.
    """
    board = _board()
    rec = _recorder(board)
    item = board.fetch_items()[0]

    rec.emit("armed", item, to="queued", status_to="In Progress")
    quoted = (
        "the record already shows this:\n\n"
        "| when | stage | kind | criteria | took | outcome |\n"
        "|---|---|---|---|---:|---|\n"
        "| x | verify | machine | — | 2s | pass |\n\n"
        "add the empty-case test"
    )
    note_body = reject_comment(1, quoted)
    board.comment(item.id, note_body)

    rec.emit("stage", item, stage=_stage(), result=StageResult(ok=True), to="reviewing", outcome="pass")

    comments = board.list_comments(item.id)
    assert len(comments) == 2
    assert comments[1]["body"] == note_body            # byte-identical, separator and all
    assert parse_reject(comments[1]["body"]) == (1, quoted)
    assert len(_rows(comments[0]["body"])) == 2        # the row went to the sticky

    board.set_number(item.id, "Bounces", 1)
    assert board.fetch_items()[0].annotation == quoted  # the producer still gets its note


def test_reject_note_survives_many_record_edits():
    """The note must outlive a whole run's worth of edits, not just one.

    A find-my-sticky that is right once but drifts to "newest" as the table grows
    (or that appends a second sticky) would still pass a single-edit test.
    """
    board = _board()
    rec = _recorder(board)
    item = board.fetch_items()[0]

    rec.emit("armed", item, to="queued", status_to="In Progress")
    note_body = reject_comment(1, "please add the empty-case test")
    board.comment(item.id, note_body)

    for _ in range(5):
        rec.emit("stage", item, stage=_stage(), result=StageResult(ok=True), to="reviewing", outcome="pass")

    comments = board.list_comments(item.id)
    assert len(comments) == 2                          # still exactly two: no sticky sprawl
    assert comments[1]["body"] == note_body
    assert len(_rows(comments[0]["body"])) == 6        # armed + 5 stages


# 2) authorship: a body someone else controls is never edited ----------------
def test_foreign_comment_is_never_edited():
    """A foreign comment carrying our marker at byte 0 must never be adopted as the
    sticky. `viewerDidAuthor` is GitHub's server-side assertion; the record's
    predicate is `is not True`, so the port must not launder a foreign comment into
    an engine-authored one. Editing it would hand an attacker the run record — and
    on a public repo, anyone can post that comment.
    """
    board = _board()
    rec = _recorder(board)
    item = board.fetch_items()[0]

    forged = "<!-- board-pilot:run item=A -->\n## board-pilot run record\n\n| when |\n|---|\n| pwn |"
    board.add_foreign_comment(item.id, forged)

    # a NON-armed event: `armed` deliberately never looks a sticky up (it opens a
    # run), so it would not exercise the filter at all.
    rec.emit("stage", item, stage=_stage(), result=StageResult(ok=True), to="reviewing", outcome="pass")

    comments = board.list_comments(item.id)
    assert len(comments) == 2                          # the engine posted its OWN sticky
    assert comments[0]["body"] == forged               # foreign body untouched
    assert comments[0]["viewerDidAuthor"] is False
    assert comments[1]["viewerDidAuthor"] is True
    assert _rows(comments[1]["body"])                  # the engine's row went to the engine's comment


def test_list_comments_preserves_unasserted_authorship(monkeypatch):
    """An ABSENT `viewerDidAuthor` must reach the record as something that is not
    True. Normalising it (`.get(k, True)`, or a `bool()` that a caller then compares
    with `== False`) would invent an assertion GitHub never made — the exact
    fail-OPEN the `is not True` predicate exists to prevent, and the same one
    `test_read_back_fails_closed_when_key_absent` pins for the reject stream.
    """
    c = _client()
    monkeypatch.setattr(c, "_issue_number", lambda item_id: 42)
    monkeypatch.setattr(
        c,
        "_run_json",
        lambda argv: {"comments": [{"id": "IC_1", "body": "<!-- board-pilot:run item=PVTI_x -->\ntrust me"}]},
    )
    assert c.list_comments("PVTI_x")[0]["viewerDidAuthor"] is not True


# 3) transport: an agent-authored body never rides argv ----------------------
def test_edit_body_rides_stdin_not_argv(monkeypatch):
    """The body is agent-authored text of unbounded length and arbitrary bytes; it
    goes over stdin, `shell=False`, exactly as `comment()` already does.

    Verified live (gh 2.96.0): `-F body=@-` reads stdin and puts it on the wire as a
    JSON *string* — the `@` file path short-circuits gh's magic type conversion, so a
    body of literally `123` or `true` stays a string instead of becoming a JSON number
    and failing the `String!` variable. The node id rides `-f` (raw), never `-F`,
    which would magic-convert an id that happened to look numeric.
    """
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input")
        captured["shell"] = kw.get("shell")
        return _Proc()

    monkeypatch.setattr(ghmod.subprocess, "run", fake_run)
    c = _client()
    body = "## board-pilot run record\n\n| when | outcome |\n|---|---|\n| x | `; rm -rf ~` |\n"
    c.edit_comment("IC_kwDOabc123", body)

    argv = captured["argv"]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert captured["input"] == body                   # body over stdin
    assert captured["shell"] is False
    assert not any(body in a for a in argv)            # …and NOWHERE on the command line
    assert not any("rm -rf" in a for a in argv)
    assert "body=@-" in argv                           # stdin wired via the field flag
    assert "-f" in argv and "id=IC_kwDOabc123" in argv

    joined = " ".join(argv)
    assert "updateIssueComment" in joined
    # `--edit-last` is the one thing this port must never do: after a reject the last
    # self-authored comment is the note.
    assert "--edit-last" not in argv


def test_list_comments_reads_the_issue_view_json(monkeypatch):
    """Same shape `_latest_reject_note` already uses — `gh issue view --json comments`
    returns `id` (a node id), `body` and `viewerDidAuthor` on every comment (verified
    live against gh 2.96.0). The port projects to exactly those three: the record
    subscripts them by name, and a wider passthrough would be a shape the Fake cannot
    honestly mirror.
    """
    captured = {}
    c = _client()
    monkeypatch.setattr(c, "_issue_number", lambda item_id: 42)

    def fake_run_json(argv):
        captured["argv"] = argv
        return {
            "comments": [
                {
                    "id": "IC_1",
                    "body": "b",
                    "viewerDidAuthor": True,
                    "author": {"login": "bridge-bot[bot]"},
                    "url": "https://…",
                }
            ]
        }

    monkeypatch.setattr(c, "_run_json", fake_run_json)
    out = c.list_comments("PVTI_x")
    assert captured["argv"] == ["gh", "issue", "view", "42", "--repo", "o/r", "--json", "comments"]
    assert out == [{"id": "IC_1", "body": "b", "viewerDidAuthor": True}]


def test_list_comments_on_a_draft_card_raises(monkeypatch):
    """A draft card has no backing issue, so it has nowhere to keep a record.
    Mirrors `comment()`, which already refuses the same way — a silent `[]` would
    make the recorder post a fresh sticky it can never find again, on every event.
    """
    c = _client()
    monkeypatch.setattr(c, "_issue_number", lambda item_id: None)
    with pytest.raises(RuntimeError) as ei:
        c.list_comments("PVTI_draft")
    assert "PVTI_draft" in str(ei.value)


def test_edit_comment_raises_when_gh_fails(monkeypatch):
    """A failed edit must RAISE, never silently no-op — otherwise the run record
    reports rows that are not on the issue, which is worse than no record.

    gh exits non-zero on a GraphQL error payload (verified live: an undefined field
    → exit 1), so the same GhCliError the rest of this adapter raises is reachable.
    `emit_guarded` at the call site is what keeps it off the termination path.
    """

    class _Proc:
        returncode = 1
        stderr = "gh: Could not resolve to a node with the global id of 'IC_bogus'"
        stdout = ""

    monkeypatch.setattr(ghmod.subprocess, "run", lambda argv, **kw: _Proc())
    c = _client()
    with pytest.raises(ghmod.GhCliError):
        c.edit_comment("IC_bogus", "body")


# 4) the Fake is only worth anything if it mirrors the real port -------------
def test_fake_and_real_agree_on_the_port(monkeypatch):
    """The Fake is the only board the suite drives end-to-end and the real client is
    never exercised offline, so a port that drifts between them proves nothing.

    This test is also the enforcement that REPLACES a BoardClient Protocol entry.
    BoardClient is the ENGINE's dependency contract, and tick.py never calls either
    method (only `comment()`); the record layer duck-types its sink on purpose so a
    board that never records stays valid. A `runtime_checkable` Protocol compares
    method PRESENCE and never signatures, so it could not catch the drift this does.
    """
    for name in ("comment", "list_comments", "edit_comment"):
        fake_fn = getattr(FakeBoardClient, name, None)
        real_fn = getattr(GhBoardClient, name, None)
        assert callable(fake_fn), f"FakeBoardClient lacks {name}"
        assert callable(real_fn), f"GhBoardClient lacks {name}"
        fake_params = list(inspect.signature(fake_fn).parameters)
        real_params = list(inspect.signature(real_fn).parameters)
        assert fake_params == real_params, f"{name}: fake{fake_params} != real{real_params}"

    # …and the same payload keys, since the record layer subscripts them by name.
    fake = _board()
    fake.comment("A", "<!-- board-pilot:run item=A -->\nx")
    fake_row = fake.list_comments("A")[0]

    real = _client()
    monkeypatch.setattr(real, "_issue_number", lambda item_id: 114)
    monkeypatch.setattr(
        real,
        "_run_json",
        lambda argv: {"comments": [{"id": "IC_1", "body": "<!-- board-pilot:run item=A -->\nx",
                                    "viewerDidAuthor": True, "author": {"login": "b[bot]"}}]},
    )
    real_row = real.list_comments("A")[0]

    assert sorted(fake_row) == sorted(real_row) == ["body", "id", "viewerDidAuthor"]
    assert fake_row["body"] == real_row["body"]
    assert fake_row["viewerDidAuthor"] is real_row["viewerDidAuthor"] is True


def test_fake_list_comments_cannot_mutate_the_store():
    """The Fake hands out copies, like `fetch_items` already does: a consumer that
    edited a returned dict would be mutating the board through a read, which the real
    remote makes impossible — and the divergence would only ever show up in prod.
    """
    board = _board()
    board.comment("A", "original")
    board.list_comments("A")[0]["body"] = "tampered"
    assert board.list_comments("A")[0]["body"] == "original"


def test_fake_edit_comment_rejects_an_unknown_id():
    """An edit to an id that is not on the board is a bug in the caller, and the Fake
    must say so rather than no-op: a silent miss here is exactly the "record reports
    rows that are not on the issue" failure the real client raises for.
    """
    board = _board()
    with pytest.raises(KeyError):
        board.edit_comment("IC_nonexistent", "body")
