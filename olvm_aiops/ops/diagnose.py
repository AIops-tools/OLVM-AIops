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
from olvm_aiops.ops.storage import GIB, list_storage_domains
from olvm_aiops.ops.vms import vm_row

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


# ─── storage capacity ───────────────────────────────────────────────────────

#: Attached-domain states that mean the domain is not serving VMs and nothing is fixing it.
SD_BROKEN = {"inactive": "The domain is inactive: VMs with disks on it cannot run.",
             "unknown": "The engine cannot determine the domain's state (usually unreachable "
                        "from the SPM host).",
             "mixed": "Hosts disagree about the domain's state — some cannot reach it."}
#: The engine or an operator is changing the domain; not a fault by itself.
SD_TRANSITIONS = {"activating", "detaching", "preparing_for_maintenance", "locked", "maintenance"}


def _sd_finding(severity: str, sd: dict, signal: str, cause: str, action: str) -> dict:
    return {"severity": severity, "storageDomainId": sd["id"], "storageDomain": sd["name"],
            "signal": signal, "cause": cause, "action": action}


def _sd_status_findings(sd: dict) -> list[dict]:
    status = sd["status"]
    if status in SD_BROKEN:
        return [_sd_finding("high", sd, f"status={status}", SD_BROKEN[status],
                            "Check the storage server and the SPM host's access to it; "
                            "activate the domain once it is reachable.")]
    if status in SD_TRANSITIONS:
        return [_sd_finding("info", sd, f"status={status}",
                            "The domain is in maintenance or a transition started by the "
                            "engine or an operator.",
                            "Re-check after the operation; investigate if it does not settle.")]
    if status is None:
        return [_sd_finding("medium", sd, "status not readable",
                            "The data center view of this attached domain could not be read "
                            "(see statusErrors), so its state is unknown here.",
                            "Check the account's permissions on the data center.")]
    return []


def _sd_capacity_findings(sd: dict) -> list[dict]:
    out = []
    free, total = sd["availableBytes"], sd["totalBytes"]
    blocker = sd["criticalSpaceBlockerGiB"]
    if free is not None and blocker is not None and free < blocker * GIB:
        out.append(_sd_finding(
            "critical", sd, f"free {free / GIB:.1f} GiB < critical blocker {blocker} GiB",
            "Below the critical-space blocker the engine refuses to create disks and snapshots "
            "on this domain.",
            "Free space (remove unused disks or snapshots) or extend the domain now."))
    elif sd["freePct"] is not None and sd["warningLowSpacePct"] is not None \
            and sd["freePct"] < sd["warningLowSpacePct"]:
        out.append(_sd_finding(
            "medium", sd,
            f"free {sd['freePct']}% < low-space warning {sd['warningLowSpacePct']}%",
            "The domain is below the engine's low-space warning threshold.",
            "Plan capacity before it reaches the critical blocker "
            f"({blocker} GiB)."))
    committed = sd["committedPctOfTotal"]
    if committed is not None and committed > 100 and total:
        out.append(_sd_finding(
            "medium", sd, f"committed {committed}% of capacity",
            "Thin-provisioned disks are promised more space than the domain holds; writes "
            "can fail once they grow.",
            "Watch actual usage, or extend the domain before guests fill their disks."))
    external = sd["externalStatus"]
    if external in ("error", "failure"):
        out.append(_sd_finding("high", sd, f"external_status={external}",
                               "An external system reported this domain as failed.",
                               "Check the external monitoring that set the status."))
    elif external == "warning":
        out.append(_sd_finding("medium", sd, "external_status=warning",
                               "An external system raised a warning on this domain.",
                               "Check the external monitoring that set the status."))
    return out


def storage_capacity_rca(conn: Any) -> dict:
    """[READ] Rank storage-domain problems (state, space, over-commit) worst first.

    Uses the data-center-scoped status of attached domains. Unattached domains
    (such as the default Glance image repository) carry no VMs and are skipped.
    """
    listing = list_storage_domains(conn, limit=u.ANALYSIS_LIST_LIMIT)
    findings: list[dict] = []
    evaluated = 0
    for sd in listing["storageDomains"]:
        if not sd["dataCenterIds"]:
            continue
        evaluated += 1
        findings += _sd_status_findings(sd)
        findings += _sd_capacity_findings(sd)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["storageDomain"] or ""))
    ranked = [{**f, "rank": i} for i, f in enumerate(findings, 1)]
    return {
        "findings": ranked,
        "domainsEvaluated": evaluated,
        "domainsSkippedUnattached": len(listing["storageDomains"]) - evaluated,
        "domainsTruncated": listing["truncated"],
        "statusErrors": listing["statusErrors"],
        "healthy": not any(f["severity"] in ("critical", "high", "medium") for f in ranked),
    }


# ─── VM health ──────────────────────────────────────────────────────────────

VM_STUCK = {
    "not_responding": "The engine lost contact with the VM's qemu process (host overload, "
                      "hung guest or a host network problem).",
    "unknown": "The engine cannot determine the VM's state — usually its host is "
               "non-responsive.",
    "paused": "The VM is paused. The engine pauses a guest on a storage I/O error or when "
              "its storage domain runs out of space, as well as on request.",
}
VM_TRANSIENT = {"migrating", "powering_up", "wait_for_launch", "powering_down",
                "reboot_in_progress", "saving_state", "restoring_state"}


def _vm_finding(severity: str, vm: dict, signal: str, cause: str, action: str) -> dict:
    return {"severity": severity, "vmId": vm["id"], "vm": vm["name"],
            "signal": signal, "cause": cause, "action": action}


def _vm_state_findings(vm: dict) -> list[dict]:
    status = vm["status"]
    detail = f" ({vm['statusDetail']})" if vm["statusDetail"] else ""
    signal = f"status={status}{detail}"
    if status in VM_STUCK:
        return [_vm_finding("high", vm, signal, VM_STUCK[status],
                            "Check the VM's recent events and its host; for paused, check the "
                            "storage domain's free space first (storage_capacity_rca).")]
    if status == "image_locked":
        return [_vm_finding("medium", vm, signal,
                            "A disk operation (create, snapshot, move) holds the VM's disks; "
                            "the VM cannot start until it finishes.",
                            "Check job_list for the running operation; investigate if it does "
                            "not finish.")]
    if status in VM_TRANSIENT:
        return [_vm_finding("info", vm, signal,
                            "The VM is changing state; not a fault by itself.",
                            "Re-check shortly; investigate if it does not settle.")]
    if status == "down" and vm["highAvailability"]:
        return [_vm_finding("high", vm, "status=down with high availability enabled",
                            "An HA VM is down. The engine restarts HA VMs that fail, so a down "
                            "HA VM was stopped on purpose or could not be restarted.",
                            "Check its events for a failed restart; start it if it was not "
                            "stopped intentionally.")]
    return []


def _superseded_by_start(vm_raw_row: dict, event: dict) -> bool:
    """True when the VM is up and its current run began after the event."""
    if vm_raw_row.get("status") != "up":
        return False
    started, happened = vm_raw_row.get("_startMs"), u.as_int(event.get("time"))
    return started is not None and happened is not None and started > happened


def vm_health_rca(conn: Any, events_limit: int = 200) -> dict:
    """[READ] Rank VM problems (stuck, paused, locked, HA down) worst first.

    Adds pending-restart configuration changes and recent warning-or-worse events
    that name an inventoried VM. Down VMs without high availability are normal.
    """
    vm_scan, vms_truncated = u.fetch_page(conn, "/vms", "vm", u.ANALYSIS_LIST_LIMIT)
    vms = [vm_row(v) for v in vm_scan]
    starts = {str(v.get("id")): u.as_int(v.get("start_time")) for v in vm_scan}
    by_id = {v["id"]: {**v, "_startMs": starts.get(v["id"])} for v in vms if v["id"]}
    ev_scan, events_truncated = u.fetch_page(
        conn, "/events", "event", events_limit, search="severity>normal sortby time desc")

    findings: list[dict] = []
    for vm in vms:
        findings += _vm_state_findings(vm)
        if vm["restartPendingForConfig"]:
            findings.append(_vm_finding(
                "low", vm, "next_run_configuration_exists=true",
                "Configuration changes are saved but apply only after the VM restarts.",
                "Restart the VM in a maintenance window to apply them."))
    for raw, ev in ((e, event_row(e)) for e in ev_scan):
        vm = by_id.get((ev.get("vm") or {}).get("id") or "")
        if vm is None:
            continue
        signal = f"event {ev['code']} {ev['severity']} at {ev['time']}: {ev['description']}"
        if _superseded_by_start(vm, raw):
            # Live lab: a start refused with "disks are locked" kept a VM that started
            # two minutes later flagged high. Old evidence is not a current fault.
            findings.append(_vm_finding(
                "info", vm, signal,
                "Superseded: the VM is up and was started after this event.",
                "No action unless it recurs; the event is kept for context."))
            continue
        findings.append(_vm_finding(
            "high" if ev["severity"] in ("error", "alert") else "medium", vm, signal,
            "The engine logged a problem for this VM.",
            "Read the event in context (event_list) and the host's vdsm log."))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["vm"] or ""))
    ranked = [{**f, "rank": i} for i, f in enumerate(findings, 1)]
    statuses: dict[str, int] = {}
    for v in vms:
        statuses[v["status"] or "unreported"] = statuses.get(v["status"] or "unreported", 0) + 1
    return {
        "findings": ranked,
        "vmsEvaluated": len(vms),
        "vmStatusCounts": statuses,
        "vmsTruncated": vms_truncated,
        "eventsEvaluated": len(ev_scan),
        "eventsTruncated": events_truncated,
        "healthy": not any(f["severity"] in ("critical", "high", "medium") for f in ranked),
    }
