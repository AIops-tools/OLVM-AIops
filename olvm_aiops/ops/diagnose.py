"""Diagnosis: one call that gathers engine state and ranks what needs attention.

Every finding carries the measured signal that produced it (the host's status
and status detail, the event code and text), a cause and an action, and an
explicit ``rank`` — worst first. States verified on a live OLVM 4.5.5 engine:
a new host passes through ``installing`` and ``reboot`` (the engine waits a
fixed 600 s after its own reboot command), and alert 9000 "Failed to verify
Power Management configuration" is raised for any host without fencing
hardware. Neither is a failure, and neither is reported as one.
"""

from __future__ import annotations

from typing import Any

from olvm_aiops.ops import _util as u
from olvm_aiops.ops.activity import event_row
from olvm_aiops.ops.inventory import host_row

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: The host cannot run or accept VMs, and the engine is not moving it anywhere.
BROKEN = {
    "non_operational": "The engine marked the host non-operational: it is reachable but "
                       "fails a cluster requirement (network, storage or CPU compatibility).",
    "non_responsive": "The engine cannot talk to vdsm on the host (network, vdsmd stopped, "
                      "certificate or time skew).",
    "install_failed": "Host deployment failed; the host never joined the cluster.",
    "error": "The engine put the host in an error state after repeated failures.",
    "down": "The host is down.",
    "kdumping": "The host kernel crashed and is writing a kdump.",
}
#: The engine is actively moving the host through a lifecycle step.
IN_PROGRESS = {"installing", "reboot", "connecting", "initializing",
               "preparing_for_maintenance", "pending_approval", "installing_os", "unassigned"}

POWER_MGMT_UNVERIFIED = 9000


def _finding(severity: str, host: dict, signal: str, cause: str, action: str) -> dict:
    return {"severity": severity, "hostId": host["id"], "host": host["name"],
            "signal": signal, "cause": cause, "action": action}


def _status_findings(host: dict) -> list[dict]:
    status = host["status"]
    detail = f" ({host['statusDetail']})" if host["statusDetail"] else ""
    signal = f"status={status}{detail}"
    if status in BROKEN:
        return [_finding("high", host, signal, BROKEN[status],
                         "Check the host's recent events (host_health_rca lists them) and "
                         "vdsmd on the host; activate it again once the cause is fixed.")]
    if status in IN_PROGRESS:
        return [_finding("info", host, signal,
                         "The engine is moving this host through a lifecycle step; it is not "
                         "failed. After an engine-driven reboot the engine waits 600 s before "
                         "reconnecting.",
                         "Re-check in a few minutes; investigate only if it stays in this state "
                         "or ends in non_operational / install_failed.")]
    if status == "maintenance":
        return [_finding("info", host, signal,
                         "The host is in maintenance: it runs no VMs by design.",
                         "Activate it when the maintenance work is done.")]
    if status is None:
        return [_finding("medium", host, "status not reported",
                         "The engine returned no status for this host.",
                         "Read the host directly (host_get) and check the engine log.")]
    return []


def _flag_findings(host: dict) -> list[dict]:
    out = []
    if host["reinstallationRequired"]:
        out.append(_finding("medium", host, "reinstallation_required=true",
                            "The host's configuration no longer matches what the engine "
                            "deployed (typically after a cluster or network change).",
                            "Put the host in maintenance and reinstall it from the engine."))
    if host["updateAvailable"]:
        out.append(_finding("low", host, "update_available=true",
                            "Newer host packages are available.",
                            "Plan an upgrade through the engine (maintenance → upgrade)."))
    return out


def _event_findings(host_by_id: dict[str, dict], events: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        ref = ev.get("host") or {}
        host = host_by_id.get(ref.get("id") or "")
        if host is None:
            continue
        signal = f"event {ev['code']} {ev['severity']} at {ev['time']}: {ev['description']}"
        if ev["code"] == POWER_MGMT_UNVERIFIED:
            out.append(_finding("low", host, signal,
                                "Power management (fencing) is not configured or not reachable. "
                                "Expected on hosts without IPMI/iLO/iDRAC; it means the engine "
                                "cannot fence this host automatically.",
                                "Configure power management if HA VMs depend on this host; "
                                "otherwise this alert is informational."))
            continue
        severity = "high" if ev["severity"] in ("error", "alert") else "medium"
        out.append(_finding(severity, host, signal,
                            "The engine logged a problem for this host.",
                            "Read the event in context (event_list) and the host's vdsm log."))
    return out


def host_health_rca(conn: Any, events_limit: int = 200) -> dict:
    """[READ] Rank what needs attention on KVM hosts, worst first.

    Combines host status, the engine's reinstall/update flags and recent
    warning-or-worse events that name a host into one list of findings.
    """
    hosts_scan, hosts_truncated = u.fetch_page(conn, "/hosts", "host", u.ANALYSIS_LIST_LIMIT)
    hosts = [host_row(h) for h in hosts_scan]
    by_id = {h["id"]: h for h in hosts if h["id"]}
    events_scan, events_truncated = u.fetch_page(
        conn, "/events", "event", events_limit, search="severity>normal sortby time desc")
    events = [event_row(e) for e in events_scan]

    findings: list[dict] = []
    for host in hosts:
        findings += _status_findings(host)
        findings += _flag_findings(host)
    findings += _event_findings(by_id, events)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["host"] or ""))
    ranked = [{**f, "rank": i} for i, f in enumerate(findings, 1)]

    statuses: dict[str, int] = {}
    for h in hosts:
        statuses[h["status"] or "unreported"] = statuses.get(h["status"] or "unreported", 0) + 1
    return {
        "findings": ranked,
        "hostsEvaluated": len(hosts),
        "hostStatusCounts": statuses,
        "hostsTruncated": hosts_truncated,
        "eventsEvaluated": len(events),
        "eventsTruncated": events_truncated,
        "healthy": not any(f["severity"] in ("critical", "high", "medium") for f in ranked),
    }
