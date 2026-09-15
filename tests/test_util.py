"""Helpers against the engine's real JSON conventions (every scalar is a string)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import _util as u

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("raw", "want"), [
    ("0", 0), ("68719476736", 68719476736), ("-1", -1), (7, 7),
    (None, None), (True, None), ("", None), ("12.5", None), ("n/a", None)])
def test_as_int_reads_engine_string_numbers(raw, want):
    got = u.as_int(raw)
    assert got == want and (got is None or type(got) is int)


def test_as_int_keeps_int64_exact():
    assert u.as_int("9223372036854775807") == 9223372036854775807


@pytest.mark.parametrize(("raw", "want"), [
    ("true", True), ("false", False), ("TRUE", True), (False, False),
    (None, None), ("", None), ("yes", None), (1, None)])
def test_as_bool_reads_engine_string_flags(raw, want):
    assert u.as_bool(raw) is want


def test_items_reads_the_singular_key_and_treats_absence_as_empty():
    assert u.items({"host": [{"id": "h1"}, "junk"]}, "host") == [{"id": "h1"}]
    assert u.items({}, "host") == []


@pytest.mark.parametrize("body", [[], "x", {"host": "not-a-list"}])
def test_items_rejects_malformed_bodies(body):
    with pytest.raises(ValueError):
        u.items(body, "host")


def test_fetch_page_measures_truncation_with_one_extra_row():
    conn = MagicMock()
    conn.get.return_value = {"event": [{"id": str(i)} for i in range(4)]}
    rows, truncated = u.fetch_page(conn, "/events", "event", 3, search="severity>normal")
    assert [r["id"] for r in rows] == ["0", "1", "2"] and truncated is True
    conn.get.assert_called_once_with("/events", params={"max": "4", "search": "severity>normal"})


def test_fetch_page_exactly_limit_rows_is_not_truncated():
    conn = MagicMock()
    conn.get.return_value = {"event": [{"id": "0"}, {"id": "1"}, {"id": "2"}]}
    rows, truncated = u.fetch_page(conn, "/events", "event", 3)
    assert len(rows) == 3 and truncated is False


@pytest.mark.parametrize("limit", [0, 1001, True, "5"])
def test_limits_are_bounded(limit):
    with pytest.raises(ValueError, match="limit"):
        u.bounded_limit(limit)


def test_pct_and_ref_id():
    assert u.pct(25, 200) == 12.5
    assert u.pct(None, 200) is None and u.pct(1, 0) is None
    assert u.ref_id({"href": "/x", "id": "abc"}) == "abc" and u.ref_id(None) is None
