"""Events and jobs, against payloads captured on a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
import re
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import activity as act

pytestmark = pytest.mark.unit

LIVE = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-installing"


def live(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def _conn(body: dict) -> MagicMock:
    conn = MagicMock()
    conn.get.return_value = body
    return conn


def test_event_rows_convert_types_and_keep_references():
    out = act.list_events(_conn(live("events")), limit=50)
    rows = out["events"]
    assert rows and all(type(r["index"]) is int and type(r["code"]) is int for r in rows)
    assert rows[0]["time"].endswith("Z")
    with_cluster = next(r for r in rows if r["cluster"])
    assert with_cluster["cluster"]["name"] == "Default"


def test_session_ids_in_login_events_are_redacted():
    """The engine prints the SSO session id in login events (code 30)."""
    event = {"index": "1", "time": 1789438559456, "severity": "normal", "code": "30",
             "description": "User admin@ovirt@internalkeycloak-authz connecting from "
                            "'192.168.60.96' using session 'EXAMPLEsessionId/x2+AA==' logged in."}
    [row] = act.list_events(_conn({"event": [event]}))["events"]
    assert "EXAMPLEsessionId" not in row["description"]
    assert "using session '[redacted]' logged in" in row["description"]


def test_severity_threshold_uses_the_search_form_that_works_live():
    conn = _conn({})
    act.list_events(conn, limit=10, min_severity="warning")
    assert conn.get.call_args.kwargs["params"] == {
        "max": "11", "search": "severity>normal sortby time desc"}


def test_normal_threshold_sends_no_severity_clause():
    conn = _conn({})
    act.list_events(conn, limit=5)
    assert conn.get.call_args.kwargs["params"]["search"] == "sortby time desc"


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"min_severity": "critical"}, "min_severity"), ({"page": 0}, "page"), ({"page": True}, "page"),
    ({"after_index": -1}, "after_index"), ({"since_minutes": 0}, "since_minutes"),
    ({"since_minutes": 5, "page": 2}, "cannot be combined")])
def test_event_arguments_are_validated(kwargs, match):
    with pytest.raises(ValueError, match=match):
        act.list_events(_conn({}), **kwargs)


def test_jobs_never_send_a_search_because_the_engine_rejects_it():
    """Live: GET /jobs?search=status=started answers HTTP 400 'bad SQL grammar'."""
    conn = _conn(live("jobs"))
    out = act.list_jobs(conn, status="started")
    params = conn.get.call_args.kwargs["params"]
    assert "search" not in params
    [job] = out["jobs"]
    assert job["status"] == "started" and job["endTime"] is None and job["durationSeconds"] is None
    assert job["description"].startswith("Adding new Host")


def test_job_status_filter_is_local_and_says_when_the_scan_was_cut():
    jobs = [{"id": str(i), "status": "finished" if i % 2 else "failed", "start_time": 1000,
             "end_time": 61000} for i in range(6)]
    out = act.list_jobs(_conn({"job": jobs}), limit=2, status="failed")
    assert [j["id"] for j in out["jobs"]] == ["0", "2"]
    assert out["truncated"] is True and out["scanTruncated"] is False
    assert out["jobs"][0]["durationSeconds"] == 60


def test_job_status_is_validated():
    with pytest.raises(ValueError, match="status"):
        act.list_jobs(_conn({}), status="running")


def test_after_index_uses_the_engine_from_cursor_that_works_live():
    """Live: from=72 returned exactly the 70 events with a higher index."""
    conn = _conn({})
    out = act.list_events(conn, limit=10, after_index=72)
    assert conn.get.call_args.kwargs["params"]["from"] == "72"
    assert out["afterIndex"] == 72


def test_since_minutes_filters_on_event_time_and_never_sends_a_time_search(monkeypatch):
    """Live: the engine's time search returned all or nothing depending on format."""
    now_ms = 1_789_440_000_000
    monkeypatch.setattr(act.time, "time", lambda: now_ms / 1000)
    events = [{"index": str(i), "severity": "normal", "code": "1",
               "time": now_ms - i * 60_000} for i in range(10)]
    conn = _conn({"event": events})
    out = act.list_events(conn, limit=50, since_minutes=5)
    params = conn.get.call_args.kwargs["params"]
    # "sortby time desc" is the sort clause; what must never be sent is a time comparison.
    assert not re.search(r"\btime\s*[<>=]", params["search"]), params["search"]
    assert params["max"] == str(act.u.ANALYSIS_LIST_LIMIT + 1)
    assert [r["index"] for r in out["events"]] == [0, 1, 2, 3, 4, 5]
    assert out["scanTruncated"] is False and out["sinceMinutes"] == 5


def test_since_minutes_reports_a_cut_scan_whose_oldest_event_is_inside_the_window(monkeypatch):
    monkeypatch.setattr(act.u, "ANALYSIS_LIST_LIMIT", 3)
    monkeypatch.setattr(act.time, "time", lambda: 2_000_000_000.0)
    events = [{"index": str(i), "time": 2_000_000_000_000} for i in range(5)]
    out = act.list_events(_conn({"event": events}), limit=50, since_minutes=10)
    assert out["returned"] == 3 and out["scanTruncated"] is True


def test_a_cut_scan_that_already_reaches_past_the_window_is_not_partial(monkeypatch):
    """Review finding: on a busy engine since_minutes=5 always said PARTIAL."""
    monkeypatch.setattr(act.u, "ANALYSIS_LIST_LIMIT", 3)
    now_ms = 2_000_000_000_000
    monkeypatch.setattr(act.time, "time", lambda: now_ms / 1000)
    events = [{"index": str(i), "time": now_ms - i * 10 * 60_000} for i in range(5)]
    out = act.list_events(_conn({"event": events}), limit=50, since_minutes=15)
    assert [r["index"] for r in out["events"]] == [0, 1]
    assert out["scanTruncated"] is False


def test_jobs_come_back_newest_first_although_the_engine_sends_oldest_first():
    """Live: /jobs orders by start time ascending, so the first rows are the OLDEST."""
    running = json.loads((LIVE.parent / "olvm-4.5.5-running" / "jobs.json").read_text())
    starts = [int(j["start_time"]) for j in running["job"]]
    assert starts == sorted(starts), "fixture premise: the engine answered oldest first"
    out = act.list_jobs(_conn(running), limit=2)
    got = [j["startTime"] for j in out["jobs"]]
    assert got == sorted(got, reverse=True)
    newest = max(running["job"], key=lambda j: int(j["start_time"]))
    assert out["jobs"][0]["id"] == newest["id"]


class PagingEngine:
    """Pages events exactly like the live engine: page size == max."""

    def __init__(self, n: int):
        self.events = [{"index": str(i), "time": 1_789_440_000_000 - i, "severity": "normal",
                        "code": "1"} for i in range(n, 0, -1)]
        self.calls: list[dict] = []

    def get(self, path, params=None):
        self.calls.append(dict(params))
        size = int(params["max"])
        search = params.get("search", "")
        page = int(search.rsplit("page ", 1)[1]) if "page " in search else 1
        start = (page - 1) * size
        return {"event": self.events[start:start + size]}


def test_paging_through_events_skips_nothing_at_page_boundaries():
    """Live: page 1 (max=5) showed 170..167 and page 2 (max=5) 165..162 — 166 was never shown."""
    engine = PagingEngine(12)
    seen, page = [], 1
    while True:
        out = act.list_events(engine, limit=4, page=page)
        seen += [r["index"] for r in out["events"]]
        if not out["truncated"]:
            break
        page += 1
    assert seen == list(range(12, 0, -1)), "every event exactly once, in order"
    assert page == 3


def test_last_page_is_not_truncated_and_a_short_page_needs_no_probe():
    engine = PagingEngine(10)
    out = act.list_events(engine, limit=4, page=3)
    assert [r["index"] for r in out["events"]] == [2, 1] and out["truncated"] is False
    assert len(engine.calls) == 1


class CursorEngine:
    """Applies `from` and the sort direction like the live engine."""

    def __init__(self, n: int):
        self.events = [{"index": str(i), "time": 1789440000000 + i, "severity": "normal",
                        "code": "1"} for i in range(1, n + 1)]

    def get(self, path, params=None):
        rows = [e for e in self.events if int(e["index"]) > int(params.get("from", -1))]
        ascending = "sortby time asc" in params.get("search", "")
        rows.sort(key=lambda e: int(e["index"]), reverse=not ascending)
        return {"event": rows[:int(params["max"])]}


def test_the_after_index_cursor_returns_the_events_right_after_it_oldest_first():
    """Live: cursor 153 with newest-first returned 173..169 and skipped 154..168 for good."""
    out = act.list_events(CursorEngine(173), limit=5, after_index=153)
    assert [r["index"] for r in out["events"]] == [154, 155, 156, 157, 158]
    assert out["truncated"] is True and out["order"] == "oldestFirst"


def test_following_the_cursor_sees_every_event_once():
    engine, cursor, seen = CursorEngine(173), 153, []
    while True:
        out = act.list_events(engine, limit=5, after_index=cursor)
        seen += [r["index"] for r in out["events"]]
        if not out["truncated"]:
            break
        cursor = max(r["index"] for r in out["events"])
    assert seen == list(range(154, 174))


def test_after_index_cannot_be_combined_with_page():
    with pytest.raises(ValueError, match="cursor"):
        act.list_events(CursorEngine(3), after_index=1, page=2)


def test_a_session_id_at_the_truncation_point_is_still_redacted():
    # The cut at 1000 falls INSIDE the id: truncating first would leave "session 'ABCD…"
    # with no closing quote, which the pattern cannot match.
    text = "x" * 970 + " using session 'ABCDEFGHIJKLMNOPQRSTUVWXYZ012345' logged in."
    assert "ABCDEFGH" not in act.redact(text, 1000)
