"""Recent activity: engine events and jobs.

Shapes and search behaviour verified on a live OLVM 4.5.5 engine:

  * events come newest first; ``search=severity>normal`` narrows by severity and
    ``sortby time desc page N`` pages; ``time`` is epoch milliseconds.
  * the engine's ``time > "<date>"`` search is NOT usable: of five date formats
    tried, two returned every event and three returned none. ``from=<index>``
    does work (events with a higher index), so "since" is offered as
    ``after_index`` (engine side) and ``since_minutes`` (filtered here on the
    event's own ``time``, over a bounded scan that reports when it was cut).
  * ``/jobs`` rejects ANY ``search`` parameter with HTTP 400 (the engine builds
    an empty SQL WHERE clause) and returns jobs OLDEST first (its own SQL orders
    by start time ascending), so jobs are read in bulk, filtered and sorted
    newest-first here — taking the first ``limit`` rows would hand back the
    oldest jobs and call them recent.
  * the login event (code 30) prints the SSO session id in its description.
    Handing that to an agent puts a live session id into its context and the
    audit trail, so session ids are redacted before any text leaves this module.
"""

from __future__ import annotations

import re
import time
from typing import Any

from olvm_aiops.ops import _util as u

#: Engine severities, least to most severe.
SEVERITIES = ("normal", "warning", "error", "alert")
JOB_STATUSES = ("started", "finished", "failed", "aborted", "unknown")

_SESSION = re.compile(r"(session\s+')[^']+(')", re.IGNORECASE)


def redact(value: Any, limit: int = 1000) -> str | None:
    """Engine text with SSO session ids removed; ``None`` when absent."""
    raw = u.text(value, limit)
    return _SESSION.sub(r"\1[redacted]\2", raw) if raw is not None else None


def _ref(value: Any) -> dict | None:
    if not isinstance(value, dict) or not value.get("id"):
        return None
    return {"id": u.text(value.get("id"), 64), "name": u.text(value.get("name"))}


def event_row(e: dict) -> dict:
    return {
        "index": u.as_int(e.get("index")),
        "time": u.ms_to_iso(e.get("time")),
        "severity": u.text(e.get("severity"), 16),
        "code": u.as_int(e.get("code")),
        "description": redact(e.get("description")),
        "correlationId": u.text(e.get("correlation_id"), 64),
        "origin": u.text(e.get("origin"), 64),
        "host": _ref(e.get("host")),
        "vm": _ref(e.get("vm")),
        "cluster": _ref(e.get("cluster")),
        "dataCenter": _ref(e.get("data_center")),
        "storageDomain": _ref(e.get("storage_domain")),
        "user": _ref(e.get("user")),
    }


def _severity_search(min_severity: str) -> str | None:
    if min_severity not in SEVERITIES:
        raise ValueError(f"min_severity must be one of {', '.join(SEVERITIES)}.")
    if min_severity == "normal":
        return None
    below = SEVERITIES[SEVERITIES.index(min_severity) - 1]
    return f"severity>{below}"


def list_events(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT, min_severity: str = "normal",
                page: int = 1, after_index: int | None = None,
                since_minutes: int | None = None) -> dict:
    """[READ] Engine events, newest first, at or above ``min_severity``.

    ``page`` walks older events. ``after_index`` returns only events newer than
    an index seen before (a cursor: pass the highest ``index`` you have).
    ``since_minutes`` keeps events whose own timestamp is within that window; it
    scans up to ``ANALYSIS_LIST_LIMIT`` newest events and ``scanTruncated`` says
    when older ones inside the window may have been missed.
    """
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer.")
    if after_index is not None and (isinstance(after_index, bool)
                                    or not isinstance(after_index, int) or after_index < 0):
        raise ValueError("after_index must be a non-negative integer.")
    if since_minutes is not None and (isinstance(since_minutes, bool)
                                      or not isinstance(since_minutes, int)
                                      or not 1 <= since_minutes <= 60 * 24 * 90):
        raise ValueError("since_minutes must be an integer between 1 and 129600 (90 days).")
    if since_minutes is not None and page > 1:
        raise ValueError("since_minutes cannot be combined with page; narrow the window instead.")
    limit = u.bounded_limit(limit)
    base = [c for c in (_severity_search(min_severity), "sortby time desc") if c]
    extra: dict[str, str] = {} if after_index is None else {"from": str(after_index)}
    meta = {"minSeverity": min_severity, "page": page, "afterIndex": after_index}

    if since_minutes is not None:
        return {**_events_since(conn, base, extra, limit, since_minutes), **meta}
    if page == 1:
        rows = u.items(conn.get("/events", params={**extra, "search": " ".join(base),
                                                   "max": str(limit + 1)}), "event")
        shown, truncated = rows[:limit], len(rows) > limit
    else:
        # The engine's page size IS max. Asking for limit+1 on page N shifts every page by
        # one row and hides the row at each boundary (verified live: with limit 4, event 166
        # never appeared). So page N is fetched with max=limit, and "more" is page N+1.
        shown = u.items(conn.get("/events", params={**extra, "max": str(limit),
                                                    "search": " ".join(base + [f"page {page}"])}),
                        "event")
        truncated = False
        if len(shown) == limit:
            nxt = conn.get("/events", params={**extra, "max": str(limit),
                                              "search": " ".join(base + [f"page {page + 1}"])})
            truncated = bool(u.items(nxt, "event"))
    out = u.envelope("events", [event_row(r) for r in shown], limit, truncated)
    return {**out, "sinceMinutes": None, "scanTruncated": None, **meta}


def _events_since(conn: Any, base: list[str], extra: dict, limit: int, since_minutes: int) -> dict:
    scan = u.items(conn.get("/events", params={**extra, "search": " ".join(base),
                                                "max": str(u.ANALYSIS_LIST_LIMIT + 1)}), "event")
    cut = len(scan) > u.ANALYSIS_LIST_LIMIT
    scan = scan[:u.ANALYSIS_LIST_LIMIT]
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)
    recent = [e for e in scan if (u.as_int(e.get("time")) or 0) >= cutoff_ms]
    # A cut scan only hides events inside the window if its oldest event is still inside it.
    oldest = u.as_int(scan[-1].get("time")) if scan else None
    scan_truncated = cut and (oldest is None or oldest >= cutoff_ms)
    out = u.envelope("events", [event_row(r) for r in recent[:limit]], limit, len(recent) > limit)
    return {**out, "sinceMinutes": since_minutes, "scanTruncated": scan_truncated}


def job_row(j: dict) -> dict:
    start, end = u.as_int(j.get("start_time")), u.as_int(j.get("end_time"))
    return {
        "id": u.text(j.get("id"), 64),
        "description": redact(j.get("description"), 500),
        "status": u.text(j.get("status"), 16),
        "startTime": u.ms_to_iso(start),
        "endTime": u.ms_to_iso(end),
        "lastUpdated": u.ms_to_iso(j.get("last_updated")),
        "durationSeconds": (end - start) // 1000 if start is not None and end is not None else None,
        "external": u.as_bool(j.get("external")),
        "autoCleared": u.as_bool(j.get("auto_cleared")),
    }


def list_jobs(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT, status: str | None = None) -> dict:
    """[READ] Engine jobs (long-running operations), newest first, optionally one status only.

    The engine refuses search and sort on ``/jobs`` and returns them oldest
    first, so up to ``ANALYSIS_LIST_LIMIT`` jobs are read, sorted by start time
    newest first and filtered here. ``scanTruncated`` means the engine held more
    jobs than that read covered — the NEWEST ones may then be missing, because
    the engine's own order puts them last.
    """
    if status is not None and status not in JOB_STATUSES:
        raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}.")
    limit = u.bounded_limit(limit)
    scan, scan_truncated = u.fetch_page(conn, "/jobs", "job", u.ANALYSIS_LIST_LIMIT)
    scan = sorted(scan, key=lambda j: u.as_int(j.get("start_time")) or 0, reverse=True)
    rows = [job_row(j) for j in scan]
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    out = u.envelope("jobs", rows[:limit], limit, len(rows) > limit)
    return {**out, "status": status, "scanTruncated": scan_truncated}
