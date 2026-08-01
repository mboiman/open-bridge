"""Prereq 4 — a dedicated Number-field reader on the gh adapter.

The reject edge stores the durable bounce counter in a board Number field. The
single-select reader (`_field_value`) does `val.get("name")` for any non-str value,
so routing a numeric through it raises AttributeError — and because `fetch_items`
runs inside the poll loop, that AttributeError would wedge the WHOLE poll every
60s. `_number_value` reads the numeric shape safely and returns an int (or None).
"""
import pytest

from engine.gh_board import GhBoardClient


def test_number_value_reads_a_flattened_numeric():
    row = {"id": "PVTI_x", "title": "t", "bounces": 3}
    assert GhBoardClient._number_value(row, "Bounces") == 3


def test_number_value_handles_zero_and_missing():
    assert GhBoardClient._number_value({"bounces": 0}, "Bounces") == 0  # present, zero
    assert GhBoardClient._number_value({"id": "x"}, "Bounces") is None  # absent → None, not crash


def test_number_value_reads_nested_number_shape():
    row = {"bounces": {"number": 5}}
    assert GhBoardClient._number_value(row, "Bounces") == 5


def test_single_select_reader_would_attributeerror_on_a_numeric():
    """Documents WHY a dedicated reader is needed: the single-select path crashes
    on a number, which inside fetch_items wedges the whole poll."""
    with pytest.raises(AttributeError):
        GhBoardClient._field_value({"bounces": 3}, "Bounces")
