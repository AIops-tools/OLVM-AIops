"""Engine health: the engine itself, not the objects it manages.

Verified on a live OLVM 4.5.5 engine: ``/ovirt-engine/services/health`` needs no
login and answers ``200 DB Up!Welcome to Health Status!`` when the engine and its
database are up; the API root carries ``product_info``, ``summary`` counts and the
engine's ``time`` in epoch milliseconds.

Engine-wide alerts — certificate expiry of the engine or its CA, missing or failed
engine backups, a cluster failing its HA reservation — name no host, VM or storage
domain, so no other diagnosis sees them. They are reported here.
"""

from __future__ import annotations

import time
from typing import Any

from olvm_aiops.connection import OlvmApiError
from olvm_aiops.ops import _util as u
from olvm_aiops.ops.activity import event_row
from olvm_aiops.ops.diagnose import (
    DEFAULT_EVENTS_WINDOW_HOURS,
    SEVERITY_ORDER,
    _window_cutoff_ms,
    classify_event,
    event_signal,
    group_events,
    recent_problem_events,
)

HEALTH_PATH = "/ovirt-engine/services/health"
#: Data-center status transitions (ovirt-engine AuditLogType 979-993, 10811): the data
#: center went non-responsive or was electing a Storage Pool Manager. They stay in the
#: log after it comes back — live, 986 "Data Center is being initialized" was still
#: there with the data center up — so they only count while the data center is not up.
DATA_CENTER_STATUS_EVENTS = {979, 980, 986, 987, 989, 992, 993, 10811}
_DC_STATUS_CAUSE = ("The data center is not up: the engine cannot reach its master storage "
                    "domain, or is electing a Storage Pool Manager (SPM).")
_DC_STATUS_ACTION = ("Check the SPM host and the hosts' access to the master storage domain "
                     "(host_health_rca, storage_capacity_rca).")
#: Beyond this, one of the two clocks is wrong enough to mislead (seconds).
CLOCK_SKEW_WARN_SECONDS = 300


def _finding(severity: str, subject: str, signal: str, cause: str, action: str) -> dict:
    return {"severity": severity, "subject": subject, "signal": signal,
            "cause": cause, "action": action}


def _counts(block: Any) -> dict:
    block = block if isinstance(block, dict) else {}
    return {"active": u.as_int(block.get("active")), "total": u.as_int(block.get("total"))}


def _health_servlet(conn: Any) -> tuple[dict, list[dict]]:
    try:
        status, body = conn.probe(HEALTH_PATH)
    except OlvmApiError as exc:
        return ({"status": None, "ok": None, "error": u.text(str(exc), 300)},
                [_finding("medium", "engine", "health servlet unreadable",
                          "The engine's own health check could not be read.",
                          "Retry; if it persists, check the engine's web server and "
                          "ovirt-engine service logs.")])
    ok = status == 200 and "DB Up" in body
    health = {"status": status, "ok": ok, "error": None}
    if ok:
        return health, []
    return health, [_finding("high", "engine", f"health servlet HTTP {status}: {u.text(body, 120)}",
                             "The engine's health check does not report its database as up.",
                             "Check the engine database (postgresql) and the ovirt-engine "
                             "service on the engine machine.")]


def _scope(ev: dict) -> tuple[str, str]:
    """(subject, noun) for an event that names no host, VM or storage domain."""
    if ev["cluster"] and ev["cluster"].get("name"):
        return f"cluster {ev['cluster']['name']}", "cluster"
    if ev["dataCenter"] and ev["dataCenter"].get("name"):
        return f"data center {ev['dataCenter']['name']}", "data center"
    return "engine", "engine"


def engine_health_rca(conn: Any, events_limit: int = 200,
                      events_window_hours: int = DEFAULT_EVENTS_WINDOW_HOURS) -> dict:
    """[READ] Rank problems with the engine itself, worst first.

    Reads the engine health servlet, the API root (version, clock, summary counts)
    and the warning-or-worse events from the last ``events_window_hours`` that name
    no host, VM or storage domain — certificate expiry, engine backups, cluster HA
    reservation — one finding per scope and event code.
    """
    _window_cutoff_ms(events_window_hours)
    health, findings = _health_servlet(conn)

    root = conn.get("")
    if not isinstance(root, dict):
        raise ValueError("The engine API root is not an object.")
    info = root.get("product_info") if isinstance(root.get("product_info"), dict) else {}
    version = info.get("version") if isinstance(info.get("version"), dict) else {}
    engine_ms = u.as_int(root.get("time"))
    skew = None if engine_ms is None else round((engine_ms - time.time() * 1000) / 1000, 1)
    if skew is not None and abs(skew) > CLOCK_SKEW_WARN_SECONDS:
        findings.append(_finding(
            "medium", "engine", f"engine clock differs from this machine by {skew:+.0f} s",
            "The engine's clock and this machine's differ by more than "
            f"{CLOCK_SKEW_WARN_SECONDS} s; one of them is wrong. A wrong engine clock breaks "
            "token lifetimes and certificate checks, and makes event times misleading.",
            "Check time synchronisation (chronyd) on the engine machine and on this machine."))

    recent, facts = recent_problem_events(conn, events_limit, events_window_hours)
    dc_scan, _ = u.fetch_page(conn, "/datacenters", "data_center", u.ANALYSIS_LIST_LIMIT)
    dc_status = {str(d["id"]): d.get("status") for d in dc_scan if d.get("id")}
    pairs, nouns = [], {}
    for raw in recent:
        ev = event_row(raw)
        if ev["host"] or ev["vm"] or ev["storageDomain"]:
            continue
        subject, noun = _scope(ev)
        nouns[subject] = noun
        pairs.append((subject, raw))
    for group in group_events(pairs):
        ev = event_row(group["latest"])
        subject, signal = group["subject"], event_signal(ev, group["count"])
        if ev["code"] in DATA_CENTER_STATUS_EVENTS:
            if dc_status.get((ev["dataCenter"] or {}).get("id")) == "up":
                findings.append(_finding("info", subject, signal,
                                         "Superseded: the data center is up now.",
                                         "No action unless it recurs; the event is kept for "
                                         "context."))
                continue
            severity = "high" if ev["severity"] in ("error", "alert") else "medium"
            findings.append(_finding(severity, subject, signal, _DC_STATUS_CAUSE,
                                     _DC_STATUS_ACTION))
            continue
        severity, cause, action = classify_event(ev, nouns[subject])
        findings.append(_finding(severity, subject, signal, cause, action))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["subject"]))
    ranked = [{**f, "rank": i} for i, f in enumerate(findings, 1)]
    summary = root.get("summary") if isinstance(root.get("summary"), dict) else {}
    return {
        "findings": ranked,
        "engineVersion": u.text(version.get("full_version"), 64),
        "healthServlet": health,
        "clockSkewSeconds": skew,
        "summary": {key: _counts(summary.get(key)) for key in ("hosts", "storage_domains", "vms")},
        **facts,
        "healthy": not any(f["severity"] in ("critical", "high", "medium") for f in ranked),
    }
