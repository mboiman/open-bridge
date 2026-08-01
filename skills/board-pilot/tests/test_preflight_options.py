"""Step 8 — every writable board option is verified LIVE at startup.

`_option_id` KeyErrors on any value that is not a live board option. That raise
happens inside the per-item dispatch loop, where the engine's outer guard swallows
it into `res.skipped` — so the item never advances and the expensive LLM stage
re-dispatches on every poll, forever, for unbounded paid spend. A missing option is
therefore not cosmetics: it is the same class of failure as a missing bounce field,
and it gets the same answer — one `field-list` read at startup that fails LOUD.

Offline: every `gh` call is monkeypatched.
"""
import pytest

from engine.gh_board import GhBoardClient

PIPELINE_VALUES = ["queued", "implementing", "verifying", "reviewing", "pr-ready", "pr-open", "parked"]
STATUS_VALUES = ["In Progress", "Blocked", "In Review"]


def _client(**kw):
    return GhBoardClient(
        project_number=32,
        owner="o",
        status_field="Workflow",
        pipeline_field="Pipeline",
        repo="o/r",
        **kw,
    )


def _board(monkeypatch, client, fields: dict):
    """Fake the live board: {field_name: [option, ...]}; absent name → KeyError."""

    def _field(name):
        if name not in fields:
            raise KeyError(f"field {name!r} not found on project 32 (o)")
        return {"id": f"FIELD_{name}", "options": {o: f"OPT_{o}" for o in fields[name]}}

    monkeypatch.setattr(client, "_field", _field)


def test_preflight_passes_when_every_writable_value_is_live(monkeypatch):
    c = _client()
    _board(monkeypatch, c, {"Pipeline": PIPELINE_VALUES, "Workflow": STATUS_VALUES})
    c.preflight_options(PIPELINE_VALUES, STATUS_VALUES)  # no raise → every value is live


def test_preflight_missing_pipeline_option_raises_naming_field_and_value(monkeypatch):
    """An operator hand-adds 7 options and fatfingers one. The error must name the
    FIELD and the VALUE — 'KeyError' three frames deep at 03:00 does not."""
    c = _client()
    _board(monkeypatch, c, {"Pipeline": [v for v in PIPELINE_VALUES if v != "parked"], "Workflow": STATUS_VALUES})
    with pytest.raises(RuntimeError) as ei:
        c.preflight_options(PIPELINE_VALUES, STATUS_VALUES)
    msg = str(ei.value)
    assert "Pipeline" in msg      # the field
    assert "parked" in msg        # the missing value
    assert "reviewing" in msg     # what IS live, so the operator sees the shape


def test_preflight_missing_status_option_raises(monkeypatch):
    """working_status / park_status / pr_status are engine-written too — a missing
    one wedges the same way. The Pipeline field being complete must not excuse it."""
    c = _client()
    _board(monkeypatch, c, {"Pipeline": PIPELINE_VALUES, "Workflow": ["In Review"]})
    with pytest.raises(RuntimeError) as ei:
        c.preflight_options(PIPELINE_VALUES, STATUS_VALUES)
    msg = str(ei.value)
    assert "Workflow" in msg
    assert "In Progress" in msg and "Blocked" in msg


def test_preflight_missing_pipeline_field_entirely_raises(monkeypatch):
    """Board #32 has NO Pipeline field at all today — the field-absent case is the
    likely one, not the exotic one, and it must not surface as a bare KeyError."""
    c = _client()
    _board(monkeypatch, c, {"Workflow": STATUS_VALUES})
    with pytest.raises(RuntimeError) as ei:
        c.preflight_options(PIPELINE_VALUES, STATUS_VALUES)
    msg = str(ei.value)
    assert "Pipeline" in msg
    assert "does not exist" in msg  # names the real problem: the FIELD, not an option


def test_preflight_names_every_missing_value_not_just_the_first(monkeypatch):
    """Report all misses at once. Naming one per run turns a 3-option gap into three
    failed startups — and the operator learns nothing about the shape of the fix."""
    c = _client()
    _board(monkeypatch, c, {"Pipeline": ["queued", "implementing"], "Workflow": STATUS_VALUES})
    with pytest.raises(RuntimeError) as ei:
        c.preflight_options(PIPELINE_VALUES, STATUS_VALUES)
    msg = str(ei.value)
    for missing in ("verifying", "reviewing", "pr-ready", "pr-open", "parked"):
        assert missing in msg


def test_preflight_with_no_values_is_a_noop(monkeypatch):
    """A pipeline that declares no writable status values must not force a
    field-list read for a field the engine never writes."""
    c = _client()

    def _never(name):
        raise AssertionError(f"must not read field {name!r} when nothing is written to it")

    monkeypatch.setattr(c, "_field", _never)
    c.preflight_options([], [])
