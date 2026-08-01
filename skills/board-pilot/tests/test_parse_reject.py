"""V1 — the reject-comment marker grammar: byte-0 anchored, CRLF tolerant.

The note parsed out of a marker is fed straight back to an autonomous code writer
as its own feedback, so "which bytes are the note" is a security boundary, not a
formatting detail. A position-blind parser lets any body that merely QUOTES a
marker steer the producer — and the read-back takes the LAST match without a
break (gh_board.py:426-428), so the newest quoting comment wins.
"""
from engine.interfaces import parse_reject, reject_comment


# 1) round-trip: what the engine writes is exactly what it reads back ---------
def test_reject_comment_round_trips():
    """reject_comment() → parse_reject() must return the round and the note
    byte-identically, or the producer reworks against mangled feedback."""
    for n in (1, 2, 3):
        note = "please add a test for the empty case\nsecond line"
        assert parse_reject(reject_comment(n, note)) == (n, note)


def test_reject_comment_round_trips_empty_note():
    # a reviewer that rejected without a reason still round-trips as that round
    assert parse_reject(reject_comment(2, "")) == (2, "")


# 2) a bot-authored record that QUOTES a reject note is not read back ---------
def test_record_quoting_a_reject_is_not_read_back():
    """The record layer posts bot-authored comments to the same issue, and a record
    entry legitimately quotes the round's reject note. The author filter cannot
    separate the two — both ARE the engine. Only byte-0 anchoring can."""
    body = (
        "<!-- board-pilot:run item=PVTI_x -->\n"
        "## board-pilot run record — #114\n"
        "\n"
        "| verify | machine | 2m08s | reject → implementing · round 1/3 |\n"
        "\n"
        "> <!-- board-pilot:reject round=1 -->\n"
        "> no negative-path test\n"
    )
    assert parse_reject(body) == (None, "")


def test_parse_reject_ignores_marker_not_at_byte_zero():
    """Prose that merely mentions a marker is not a reject note. Markdown quotes
    always carry a `> ` prefix, so anchoring at byte 0 beats the quoting case
    structurally rather than by escaping."""
    body = (
        "Heads-up: a stale <!-- board-pilot:reject round=9 --> marker sits in the trail\n"
        "second line"
    )
    assert parse_reject(body) == (None, "")


# 3) GitHub delivers comment bodies with CRLF --------------------------------
def test_parse_reject_tolerates_crlf():
    assert parse_reject("<!-- board-pilot:reject round=1 -->\r\nadd the negative-path test") == (
        1,
        "add the negative-path test",
    )


def test_parse_reject_crlf_note_is_carried_verbatim():
    """Only the marker's own terminator is consumed — the note is DATA and keeps
    its own line endings, so what the reviewer wrote is what the producer reads."""
    assert parse_reject("<!-- board-pilot:reject round=2 -->\r\nline one\r\nline two") == (
        2,
        "line one\r\nline two",
    )
