"""Diagnosis: one call that gathers engine state and ranks what needs attention.

Every finding carries the measured signal that produced it (the host's status
and status detail, the event code and text), a cause and an action, and an
explicit ``rank`` — worst first. States verified on a live OLVM 4.5.5 engine:
a new host passes through ``installing`` and ``reboot`` (the engine waits a
fixed 600 s after its own reboot command), and alert 9000 "Failed to verify
Power Management configuration" is raised for any host without fencing
hardware. Neither is a failure, and neither is reported as one.

Events are shared evidence, and each warning-or-worse event inside the window
belongs to exactly one diagnosis: an event naming a VM to ``vm_health_rca``, one
naming a storage domain (and no VM) to ``storage_capacity_rca``, one naming only a
host to ``host_health_rca``, and one naming none of those to
``engine_health_rca``. Nothing is dropped for lacking a host or VM — that is how
certificate-expiry and no-backup alerts used to vanish from every diagnosis.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from olvm_aiops.connection import OlvmApiError
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
#: AuditLogType.VDS_BROKER_COMMAND_FAILURE — "VDSM ${VdsName} command ${CommandName} failed:
#: ${message}". A wrapper around one vdsm call, logged against the host that ran it: the
#: command name, not the host, says what the failure was about.
VDSM_COMMAND_FAILURE = 10802
#: vdsm verbs that call into a VM's guest agent (VDSCommandType VmLogon/VmLogoff, logged as
#: VmLogonVDS/VmLogoffVDS). They fail when the guest agent is missing or not responding,
#: which is a condition of that guest, not of the host.
GUEST_AGENT_COMMANDS = ("VmLogon", "VmLogoff")
#: The engine's message for the guest-agent condition ("Guest agent non-responsive"); the
#: description is lowercased and its hyphens turned into spaces first, so "Guest-agent" matches
#: too. The same command can fail for a transport reason ("Message timeout which can be caused
#: by communication issues"), which IS about the host's link to vdsm — so the command alone
#: must not downgrade the finding. Both this and the command are read out of the engine's
#: English message; on an engine serving a translated AuditLogMessages bundle neither matches,
#: and the event stays a plain host finding (over-report, never under-report).
GUEST_AGENT_MESSAGE = "guest agent"
#: Most candidate VMs listed on a guest-scoped finding.
VM_CANDIDATE_LIMIT = 10

_COMMAND_FAILED = re.compile(r"command (\w+) failed", re.IGNORECASE)
#: ovirt-engine AuditLogType codes of the normal-severity events that record a recovery.
HOST_STATUS_SET = 13           # VDS_DETECTED: "Status of host X was set to <status>."
VM_STATUS_RESTORED = 163       # "VM X status was restored to <status>."
VM_RECOVERED_FROM_PAUSE = 196  # "VM X has recovered from paused back to up."

PROBLEM_SEVERITIES = ("warning", "error", "alert")
#: Events older than this are history, not current evidence (hours).
DEFAULT_EVENTS_WINDOW_HOURS = 24
MAX_EVENTS_WINDOW_HOURS = 24 * 30

_HOST_CERT_SOON = ("The host's certificate expires soon (the event text has the date). An "
                   "expired host certificate leaves the host non-responsive.",
                   "Put the host in maintenance and enroll a new certificate "
                   "(Enroll Certificate), or reinstall it.")
_ENGINE_CERT_SOON = ("The engine's certificate expires soon (the event text has the date).",
                     "Run engine-setup on the engine machine to renew its certificates.")
_CA_CERT_SOON = ("The engine CA certificate expires soon (the event text has the date); every "
                 "certificate it signed stops being trusted when it does.",
                 "Renew the CA with engine-setup, then re-enroll host certificates.")

#: Audit-log codes whose meaning is known (ovirt-engine AuditLogType and
#: AuditLogMessages), as (severity, cause, action). Each describes a standing
#: condition the engine re-raises — most at most once a day — so a later recovery
#: event does not supersede it.
KNOWN_EVENTS: dict[int, tuple[str, str, str]] = {
    845: ("medium", *_HOST_CERT_SOON),
    879: ("high", *_HOST_CERT_SOON),
    877: ("critical", "The host's certificate has expired: the engine can no longer talk to "
                      "vdsm on it securely.",
          "Put the host in maintenance and enroll a new certificate now."),
    876: ("high", "The host's certificate is invalid.", "Enroll a new certificate for the host."),
    889: ("high", "The host's certificate has an invalid subject alternative name.",
          "Enroll a new certificate for the host."),
    847: ("medium", *_ENGINE_CERT_SOON),
    878: ("high", *_ENGINE_CERT_SOON),
    846: ("critical", "The engine's certificate has expired: clients and hosts no longer trust "
                      "it.", "Run engine-setup on the engine machine to renew it now."),
    849: ("medium", *_CA_CERT_SOON),
    883: ("high", *_CA_CERT_SOON),
    848: ("critical", "The engine CA certificate has expired: no certificate it signed is "
                      "trusted any more.",
          "Renew the CA with engine-setup and re-enroll every host certificate."),
    9022: ("medium", "The engine has no full backup.",
           "Run engine-backup and keep the file off the engine machine."),
    9023: ("medium", "The last full engine backup is older than the engine's limit.",
           "Run engine-backup."),
    9026: ("high", "An engine backup failed.",
           "Read the engine-backup log, fix the cause and run it again."),
    10300: ("medium", "The cluster failed the HA reservation check: if a host fails, its HA "
                      "VMs may have nowhere to restart.",
            "Add capacity to the cluster or reduce the resources reserved by HA VMs."),
    970: ("high", "The engine deactivated this storage domain because no host could see it.",
          "Restore the hosts' access to the storage server, then activate the domain."),
    604: ("medium", "The host's clock drifts beyond the engine's allowed maximum; certificate "
                    "checks and scheduling can fail.",
          "Fix time synchronisation (chronyd) on the host."),
}


# ─── shared event evidence ──────────────────────────────────────────────────


def _window_cutoff_ms(hours: Any) -> int:
    if isinstance(hours, bool) or not isinstance(hours, int) \
            or not 1 <= hours <= MAX_EVENTS_WINDOW_HOURS:
        raise ValueError(f"events_window_hours must be an integer between 1 and "
                         f"{MAX_EVENTS_WINDOW_HOURS}.")
    return int((time.time() - hours * 3600) * 1000)


def recent_problem_events(conn: Any, events_limit: Any,
                          events_window_hours: Any) -> tuple[list[dict], dict]:
    """Warning-or-worse events inside the window (raw, newest first) and the scan facts.

    ``eventsTruncated`` is true only when the scan was cut while its oldest event was
    still inside the window — a busy engine holds more than ``events_limit`` warnings
    over its whole retention, and that alone says nothing about the window.
    """
    limit = u.bounded_limit(events_limit, "events_limit")
    cutoff_ms = _window_cutoff_ms(events_window_hours)
    scan, cut = u.fetch_page(conn, "/events", "event", limit,
                             search="severity>normal sortby time desc")
    # The search asks for warning or worse; hold to that even if it was not applied.
    scan = [e for e in scan if e.get("severity") in PROBLEM_SEVERITIES]
    recent = [e for e in scan if (u.as_int(e.get("time")) or 0) >= cutoff_ms]
    times = [t for t in (u.as_int(e.get("time")) for e in scan) if t is not None]
    oldest = min(times) if times else None
    return recent, {
        "eventsEvaluated": len(recent),
        "eventsOutsideWindow": len(scan) - len(recent),
        "eventsWindowHours": events_window_hours,
        "eventsTruncated": cut and (oldest is None or oldest >= cutoff_ms),
    }


def _is_recovery(code: int | None, description: Any) -> bool:
    text = str(description or "")
    if code == HOST_STATUS_SET:
        return "set to Up" in text
    if code == VM_STATUS_RESTORED:
        return "restored to Up" in text
    return code == VM_RECOVERED_FROM_PAUSE


def _recovery_times(conn: Any, codes: set[int], cutoff_ms: int,
                    ref_key: str) -> dict[str, list[int]]:
    """Times of recovery events inside the window, per host or VM id."""
    search = " or ".join(f"type={c}" for c in sorted(codes)) + " sortby time desc"
    rows, _ = u.fetch_page(conn, "/events", "event", u.ANALYSIS_LIST_LIMIT, search=search)
    out: dict[str, list[int]] = {}
    for e in rows:
        code, happened = u.as_int(e.get("code")), u.as_int(e.get("time"))
        ref = u.ref_id(e.get(ref_key))
        if code not in codes or happened is None or happened < cutoff_ms or not ref:
            continue
        if _is_recovery(code, e.get("description")):
            out.setdefault(ref, []).append(happened)
    return out


def _event_subkey(raw: dict) -> str | None:
    """What splits a code into separate conditions. Only event 10802 has one: it wraps any
    vdsm command, so grouping the whole code together would let the newest member classify
    a `SpmStatusVDS` failure as whatever the newest `VmLogonVDS` was.

    The command alone is not enough: one command reports two different conditions, and
    ``VmLogonVDS failed: Message timeout`` (the host's link to vdsm) must not be collapsed
    into a newer ``VmLogonVDS failed: Guest agent non-responsive``. The key is therefore the
    condition — the command, plus whether this is the guest-agent one — and it is read from
    the same text the classifier sees.
    """
    if u.as_int(raw.get("code")) != VDSM_COMMAND_FAILURE:
        return None
    description = event_row(raw)["description"]
    command = vdsm_command(description)
    if command is None:
        return None
    return f"{command}|guest-agent" if guest_agent_command(description) else command


def group_events(pairs: list[tuple[str, dict]]) -> list[dict]:
    """One group per subject, event code and (for wrapper codes) command: count and latest."""
    groups: dict[tuple[str, int | None, str | None], dict] = {}
    for subject, raw in pairs:
        happened = u.as_int(raw.get("time")) or 0
        key = (subject, u.as_int(raw.get("code")), _event_subkey(raw))
        group = groups.get(key)
        if group is None:
            groups[key] = {"subject": subject, "latest": raw, "time": happened, "count": 1}
            continue
        group["count"] += 1
        if happened > group["time"]:
            group.update(latest=raw, time=happened)
    return list(groups.values())


def event_signal(ev: dict, count: int) -> str:
    repeat = f" (×{count} in window)" if count > 1 else ""
    return f"event {ev['code']} {ev['severity']} at {ev['time']}{repeat}: {ev['description']}"


def classify_event(ev: dict, subject: str) -> tuple[str, str, str]:
    """(severity, cause, action) for an event: the catalogue first, then its severity."""
    known = KNOWN_EVENTS.get(ev["code"])
    if known is not None:
        return known
    return ("high" if ev["severity"] in ("error", "alert") else "medium",
            f"The engine logged a problem for this {subject}.",
            "Read the event in context (event_list) and the engine and vdsm logs.")


def vdsm_command(description: Any) -> str | None:
    """The command name out of a VDS_BROKER_COMMAND_FAILURE description, if it is there."""
    match = _COMMAND_FAILED.search(str(description or ""))
    return match.group(1) if match else None


def guest_agent_command(description: Any) -> str | None:
    """The guest-agent command this description reports, when it reports one.

    Both halves of the engine's message are needed: the command says the call went into a
    VM, the message says the guest agent is what failed. Either alone is not the condition.
    """
    command = vdsm_command(description)
    if command is None or not command.startswith(GUEST_AGENT_COMMANDS):
        return None
    normalised = str(description or "").lower().replace("-", " ")
    return command if GUEST_AGENT_MESSAGE in normalised else None


def guest_agent_failure(ev: dict) -> tuple[str, str, str] | None:
    """(severity, cause, action) when this event is a guest-agent call that failed.

    Live production feedback (#1): 10802 "VmLogonVDS failed: Guest agent non-responsive"
    was ranked ``high`` against hosts that were up, ``externalStatus: ok`` and needed
    neither an update nor a reinstall — the event is about a guest, and the engine
    attributes it to the vdsm host only because that is where the call ran.
    """
    if ev["code"] != VDSM_COMMAND_FAILURE:
        return None
    # A guest-agent verb that failed for another reason (a vdsm transport timeout, say) is
    # not evidence that the host is fine; it stays on the normal host path.
    command = guest_agent_command(ev["description"])
    if command is None:
        return None
    return ("low",
            f"The vdsm command {command} failed on this host, and the engine's message names "
            "the guest agent. It is a call into a VM's guest agent, so this reports an agent "
            "that is missing or not responding — not a fault of the host, whose own state is "
            "reported by its status, external status and flags. A VM without a responsive "
            "guest agent still runs, but reports no in-guest data and cannot be logged into "
            "from the console.",
            "Check the guest agent (ovirt-guest-agent / qemu-guest-agent) inside the VM. "
            "The event names no VM, so vmCandidates lists the VMs the engine reports on "
            "this host: candidates to check, not a confirmed mapping. For the full list on "
            "a busy host, read vm_list with search='host=<name>'.")


def _ranked(findings: list[dict], subject_key: str) -> list[dict]:
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f[subject_key] or ""))
    return [{**f, "rank": i} for i, f in enumerate(findings, 1)]


def _healthy(findings: list[dict]) -> bool:
    return not any(f["severity"] in ("critical", "high", "medium") for f in findings)


# ─── hosts ──────────────────────────────────────────────────────────────────


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
                         "reconnecting. This diagnosis cannot tell how long the host has been "
                         "in this state.",
                         "Re-check in a few minutes; if it has not moved on, read its events "
                         "(event_list) — a host stuck here needs attention.")]
    if status == "maintenance":
        return [_finding("info", host, signal,
                         "The host is in maintenance: it runs no VMs by design.",
                         "Activate it when the maintenance work is done.")]
    if status is None:
        return [_finding("medium", host, "status not reported",
                         "The engine returned no status for this host.",
                         "Read the host directly (host_get) and check the engine log.")]
    return []


def _external_findings(host: dict) -> list[dict]:
    external = host["externalStatus"]
    if external in ("error", "failure"):
        return [_finding("high", host, f"external_status={external}",
                         "An external system reported this host as failed.",
                         "Check the external monitoring that set the status.")]
    if external == "warning":
        return [_finding("medium", host, "external_status=warning",
                         "An external system raised a warning on this host.",
                         "Check the external monitoring that set the status.")]
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


def vm_candidates(conn: Any) -> Callable[[str], dict]:
    """A lookup of the VMs the engine reports on a host, read once and reused.

    A failed read is reported as ``error`` with ``total: null``: "the VMs could not be
    read" must not look like "this host runs nothing". host_health_rca does not otherwise
    need the VM list, so a missing permission narrows one finding instead of failing the
    whole diagnosis. ``scanTruncated`` says the VM scan itself was cut, which makes both
    the list and ``total`` a lower bound — a candidate list that was cut must say so.
    """
    cache: dict[str, Any] = {}

    def lookup(host_id: str) -> dict:
        if not cache:
            try:
                rows, cut = u.fetch_page(conn, "/vms", "vm", u.ANALYSIS_LIST_LIMIT)
                cache.update(rows=[vm_row(v) for v in rows], error=None, cut=cut)
            except (OlvmApiError, ValueError) as exc:
                # Engine text reaches the model: sanitise it like every other engine string.
                # An exception with no message still has to say something — an empty string
                # would read as "no error" to anything testing this field for truth.
                reason = u.text(str(exc), 300) or f"{type(exc).__name__} without a message"
                cache.update(rows=[], error=reason, cut=False)
        on_host = sorted(({"id": v["id"], "name": v["name"], "status": v["status"]}
                          for v in cache["rows"] if v["hostId"] == host_id),
                         key=lambda v: v["name"] or "")
        shown = on_host[:VM_CANDIDATE_LIMIT]
        failed = cache["error"] is not None
        return {**u.envelope("vms", shown, VM_CANDIDATE_LIMIT,
                             None if failed else len(on_host) > VM_CANDIDATE_LIMIT),
                # A read that failed has no scan to describe: null, not "nothing was cut".
                "total": None if failed else len(on_host),
                "scanTruncated": None if failed else cache["cut"],
                "error": cache["error"]}

    return lookup


def _event_findings(host_by_id: dict[str, dict], raw_events: list[dict],
                    set_up_at: dict[str, list[int]],
                    candidates: Callable[[str], dict]) -> list[dict]:
    """Findings for events about a host alone, one per host and event code.

    An event that also names a VM or a storage domain is about that object; the host
    it carries is only where the operation ran (live: "Storage Domain … attached"
    carries the executing host).
    """
    pairs = []
    for raw in raw_events:
        ev = event_row(raw)
        if ev["vm"] or ev["storageDomain"]:
            continue
        host = host_by_id.get((ev["host"] or {}).get("id") or "")
        if host is not None:
            pairs.append((host["id"], raw))
    out = []
    for group in group_events(pairs):
        host, ev = host_by_id[group["subject"]], event_row(group["latest"])
        signal = event_signal(ev, group["count"])
        if ev["code"] == POWER_MGMT_UNVERIFIED:
            out.append(_finding("low", host, signal,
                                "Power management (fencing) is not configured or not reachable. "
                                "Expected on hosts without IPMI/iLO/iDRAC; it means the engine "
                                "cannot fence this host automatically.",
                                "Configure power management if HA VMs depend on this host; "
                                "otherwise this alert is informational."))
            continue
        guest = guest_agent_failure(ev)
        if guest is not None:
            # Checked before the supersede rule: the host never went down, so "the engine
            # set it back to Up" says nothing about the guest agent.
            out.append({**_finding(guest[0], host, signal, *guest[1:]),
                        "vmCandidates": candidates(host["id"])})
            continue
        recovered = any(t > group["time"] for t in set_up_at.get(host["id"], []))
        if ev["code"] not in KNOWN_EVENTS and host["status"] == "up" and recovered:
            out.append(_finding("info", host, signal,
                                "Superseded: the engine set this host to Up after this event, "
                                "and it is up now.",
                                "No action unless it recurs; the event is kept for context."))
            continue
        severity, cause, action = classify_event(ev, "host")
        out.append(_finding(severity, host, signal, cause, action))
    return out


def host_health_rca(conn: Any, events_limit: int = 200,
                    events_window_hours: int = DEFAULT_EVENTS_WINDOW_HOURS) -> dict:
    """[READ] Rank what needs attention on KVM hosts, worst first.

    Combines host status, external status, the engine's reinstall/update flags and
    warning-or-worse events from the last ``events_window_hours`` about a host alone
    (repeats collapsed). A problem event is superseded when the engine set the host
    to Up afterwards and it is up now; certificate and time-drift alerts are standing
    conditions and are never superseded.

    A vdsm guest-agent call that failed (event 10802 VmLogonVDS / VmLogoffVDS) is a
    condition of a guest, not of the host that ran the call: it is reported ``low`` and
    carries ``vmCandidates``, the VMs the engine reports on that host. The event names
    no VM, so those are candidates to check, never a confirmed mapping.
    """
    cutoff_ms = _window_cutoff_ms(events_window_hours)
    hosts_scan, hosts_truncated = u.fetch_page(conn, "/hosts", "host", u.ANALYSIS_LIST_LIMIT)
    hosts = [host_row(h) for h in hosts_scan]
    by_id = {h["id"]: h for h in hosts if h["id"]}
    recent, facts = recent_problem_events(conn, events_limit, events_window_hours)
    set_up_at = _recovery_times(conn, {HOST_STATUS_SET}, cutoff_ms, "host")

    findings: list[dict] = []
    for host in hosts:
        findings += _status_findings(host)
        findings += _external_findings(host)
        findings += _flag_findings(host)
    findings += _event_findings(by_id, recent, set_up_at, vm_candidates(conn))
    ranked = _ranked(findings, "host")

    statuses: dict[str, int] = {}
    for h in hosts:
        statuses[h["status"] or "unreported"] = statuses.get(h["status"] or "unreported", 0) + 1
    return {
        "findings": ranked,
        "hostsEvaluated": len(hosts),
        "hostStatusCounts": statuses,
        "hostsTruncated": hosts_truncated,
        **facts,
        "healthy": _healthy(ranked),
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


def _sd_status_findings(sd: dict, view_timed_out: bool) -> list[dict]:
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
        if view_timed_out:
            return [_sd_finding("medium", sd, "status not readable (data center view timed out)",
                                "The domain is attached, but reading its data center's "
                                "storage view timed out, so its state is unknown here.",
                                "Retry; if the engine is busy, raise 'timeout' for this target.")]
        return [_sd_finding("medium", sd, "status not readable",
                            "The domain is attached, but no status for it came back from its "
                            "data center — the data center could not be read (statusErrors), "
                            "or its storage-domain list did not include this domain.",
                            "Check the account's permissions on the data center, then read "
                            "the domain again (storage_domain_get).")]
    return []


def _crossable(threshold: int | None) -> bool:
    """A threshold the engine can actually cross. It reports 0 for a domain with none set,
    and ``free < 0`` / ``free% < 0`` is never true — 0 is "no threshold", not one that
    keeps passing."""
    return threshold is not None and threshold > 0


def _sd_threshold_findings(sd: dict) -> list[dict]:
    """The engine has no threshold here, so nothing can warn before the domain fills.

    Silence would say the opposite. This states the gap and carries the measured free
    space; it does not invent a threshold of its own — how full is too full is the
    operator's policy, and this diagnosis has no way to read it.
    """
    warning, blocker = sd["warningLowSpacePct"], sd["criticalSpaceBlockerGiB"]
    if _crossable(warning):
        return []
    free_pct = sd["freePct"]
    measured = f"free {free_pct}%" if free_pct is not None else "free space not reported"
    if _crossable(blocker):
        return [_sd_finding(
            "low", sd,
            f"no low-space warning set; only the critical blocker ({blocker} GiB); {measured}",
            "The domain has no low-space warning (warning_low_space_indicator is 0 or absent), "
            f"so the first and only signal is the critical blocker at {blocker} GiB — the "
            "point where the engine already refuses new disks and snapshots. There is no "
            "earlier signal, for the engine or for this diagnosis.",
            "Set warning_low_space_indicator on the domain so it warns before the blocker, "
            "and watch the free space reported here meanwhile.")]
    return [_sd_finding(
        "low", sd, f"no low-space threshold set; {measured}",
        "Neither warning_low_space_indicator nor critical_space_action_blocker is set on this "
        "domain (the engine reports 0 for both), so neither the engine nor this diagnosis "
        "can warn before it fills: no amount of free space will raise a finding here. How "
        "full is too full is your policy, and it is not recorded on the domain.",
        "Set warning_low_space_indicator and critical_space_action_blocker on the domain, and "
        "judge the free space in this signal yourself until then.")]


def _sd_capacity_findings(sd: dict) -> list[dict]:
    out = _sd_threshold_findings(sd)
    free, total = sd["availableBytes"], sd["totalBytes"]
    blocker, warning = sd["criticalSpaceBlockerGiB"], sd["warningLowSpacePct"]
    # Compare the exact ratio: the rounded freePct turns 9.96 % into 10.0 % and misses a
    # 10 % threshold.
    free_ratio = free / total * 100 if free is not None and total else None
    below_blocker = free is not None and blocker is not None and free < blocker * GIB
    below_warning = (not below_blocker and free_ratio is not None and warning is not None
                     and free_ratio < warning)
    if below_blocker:
        out.append(_sd_finding(
            "critical", sd, f"free {free / GIB:.1f} GiB < critical blocker {blocker} GiB",
            "Below the critical-space blocker the engine refuses to create disks and snapshots "
            "on this domain.",
            "Free space (remove unused disks or snapshots) or extend the domain now."))
    if below_warning:
        # str(None) must never reach the text: an engine that reports no blocker would
        # otherwise be quoted a threshold of "None GiB".
        blocker_text = (f" the critical blocker ({blocker} GiB)" if blocker is not None
                        else " the critical blocker")
        out.append(_sd_finding(
            "medium", sd, f"free {free_ratio:.2f}% < low-space warning {warning}%",
            "The domain is below the engine's low-space warning threshold.",
            f"Plan capacity before it reaches{blocker_text}."))
    committed = sd["committedPctOfTotal"]
    if committed is not None and committed > 100 and total:
        # Over-commit is normal for thin, template-based deployments; it is only urgent
        # once space is also running low. Live production feedback (#1): a domain at 192 %
        # committed and 43.7 % used read as a problem, so the finding now carries actual
        # use and says which of the two it is.
        # Read from the conditions, never from the other findings' text: a finding that
        # merely mentions the blocker (the missing-threshold one does) is not low space.
        low_space = below_blocker or below_warning
        used_pct, free_pct = sd["usedPct"], sd["freePct"]
        actual = (f", in use {used_pct}% ({free_pct}% free)"
                  if used_pct is not None and free_pct is not None else "")
        promised = ("Thin-provisioned disks are promised more space than the domain holds. ")
        # "Not under pressure" may only be claimed when a threshold was actually compared.
        # The engine reports 0 for a domain with no low-space warning set (live: the image
        # repository), and 0 can never be crossed — that is not a passed check.
        checked = _crossable(warning) or _crossable(blocker)
        if low_space:
            cause = (promised[:-2] + ", and the domain is already low on space: the promise "
                     "cannot be kept, and a guest that grows into it now can fail its writes.")
            action = ("Free space or extend the domain: over-commit is only safe while free "
                      "space lasts.")
        elif checked:
            cause = (promised + "Free space is still above what the engine set for this "
                     "domain, so this is a planning limit, not current pressure: writes fail "
                     "only once guests grow into what was promised.")
            if _crossable(warning):
                limit = f"its low-space threshold ({warning}% free)"
            else:
                # Only the hard stop exists: say so, and do not call it an early warning.
                limit = (f"the critical blocker ({blocker} GiB free), where the engine refuses "
                         "new disks and snapshots — no low-space warning is set for this "
                         "domain, so there will be no earlier signal")
            action = ("No action while free space holds. Track how fast actual use grows and "
                      f"extend the domain before it reaches {limit}.")
        else:
            cause = (promised + "The engine reports no low-space threshold for this domain "
                     "(warning_low_space_indicator and critical_space_action_blocker are 0 or "
                     "absent), so no check was made and this diagnosis cannot say whether the "
                     "space left is comfortable — read the free space in the signal.")
            action = ("Set the domain's low-space warning and critical blocker in the engine "
                      "so it can raise one, and track how fast actual use grows.")
        out.append(_sd_finding("medium" if low_space else "low", sd,
                               f"committed {committed}% of capacity{actual}", cause, action))
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


def storage_capacity_rca(conn: Any, events_limit: int = 200,
                         events_window_hours: int = DEFAULT_EVENTS_WINDOW_HOURS) -> dict:
    """[READ] Rank storage-domain problems (state, space, over-commit, events) worst first.

    Uses the data-center-scoped status of attached domains. Unattached domains
    (such as the default Glance image repository) carry no VMs and are skipped.
    Warning-or-worse events from the last ``events_window_hours`` that name a storage
    domain and no VM are reported against that domain.
    """
    _window_cutoff_ms(events_window_hours)
    listing = list_storage_domains(conn, limit=u.ANALYSIS_LIST_LIMIT)
    timed_out_dcs = {e["dataCenterId"] for e in listing["statusErrors"] if e.get("timedOut")}
    findings: list[dict] = []
    evaluated = 0
    for sd in listing["storageDomains"]:
        if not sd["dataCenterIds"]:
            continue
        evaluated += 1
        findings += _sd_status_findings(sd, bool(timed_out_dcs & set(sd["dataCenterIds"])))
        findings += _sd_capacity_findings(sd)

    recent, facts = recent_problem_events(conn, events_limit, events_window_hours)
    by_id = {sd["id"]: sd for sd in listing["storageDomains"] if sd["id"]}
    pairs = []
    for raw in recent:
        ev = event_row(raw)
        sd_id = (ev["storageDomain"] or {}).get("id")
        if not ev["vm"] and sd_id in by_id:
            pairs.append((sd_id, raw))
    for group in group_events(pairs):
        ev = event_row(group["latest"])
        severity, cause, action = classify_event(ev, "storage domain")
        findings.append(_sd_finding(severity, by_id[group["subject"]],
                                    event_signal(ev, group["count"]), cause, action))

    ranked = _ranked(findings, "storageDomain")
    return {
        "findings": ranked,
        "domainsEvaluated": evaluated,
        "domainsSkippedUnattached": len(listing["storageDomains"]) - evaluated,
        "domainsTruncated": listing["truncated"],
        "statusErrors": listing["statusErrors"],
        **facts,
        "healthy": _healthy(ranked),
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
    """True when the VM started after the event, whatever its state now.

    A start after a failure proves the failure no longer holds; a later, normal
    shutdown does not bring it back.
    """
    started, happened = vm_raw_row.get("_startMs"), u.as_int(event.get("time"))
    return started is not None and happened is not None and started > happened


def vm_health_rca(conn: Any, events_limit: int = 200,
                  events_window_hours: int = DEFAULT_EVENTS_WINDOW_HOURS) -> dict:
    """[READ] Rank VM problems (stuck, paused, locked, HA down) worst first.

    Adds pending-restart configuration changes and warning-or-worse events from the
    last ``events_window_hours`` that name an inventoried VM, one finding per VM and
    event code. An event is superseded when the VM started after it, or when the
    engine reported the VM back up (recovered from pause, status restored) and it is
    up now. Down VMs without high availability are normal.
    """
    cutoff_ms = _window_cutoff_ms(events_window_hours)
    vm_scan, vms_truncated = u.fetch_page(conn, "/vms", "vm", u.ANALYSIS_LIST_LIMIT)
    vms = [vm_row(v) for v in vm_scan]
    starts = {str(v.get("id")): u.as_int(v.get("start_time")) for v in vm_scan}
    by_id = {v["id"]: {**v, "_startMs": starts.get(v["id"])} for v in vms if v["id"]}
    recent, facts = recent_problem_events(conn, events_limit, events_window_hours)
    back_up_at = _recovery_times(conn, {VM_RECOVERED_FROM_PAUSE, VM_STATUS_RESTORED},
                                 cutoff_ms, "vm")

    findings: list[dict] = []
    for vm in vms:
        findings += _vm_state_findings(vm)
        if vm["restartPendingForConfig"]:
            findings.append(_vm_finding(
                "low", vm, "next_run_configuration_exists=true",
                "Configuration changes are saved but apply only after the VM restarts.",
                "Restart the VM in a maintenance window to apply them."))
    pairs = [(vm_id, raw) for raw in recent
             if (vm_id := u.ref_id(raw.get("vm"))) is not None and vm_id in by_id]
    for group in group_events(pairs):
        vm, raw = by_id[group["subject"]], group["latest"]
        ev = event_row(raw)
        signal = event_signal(ev, group["count"])
        if _superseded_by_start(vm, raw):
            # Live lab: a start refused with "disks are locked" kept a VM that started
            # two minutes later flagged high. Old evidence is not a current fault.
            findings.append(_vm_finding(
                "info", vm, signal, "Superseded: the VM was started after this event.",
                "No action unless it recurs; the event is kept for context."))
            continue
        if vm["status"] == "up" and any(t > group["time"] for t in back_up_at.get(vm["id"], [])):
            findings.append(_vm_finding(
                "info", vm, signal,
                "Superseded: the engine reported the VM back up after this event (recovered "
                "from pause or status restored), and it is up now.",
                "No action unless it recurs; the event is kept for context."))
            continue
        severity, cause, action = classify_event(ev, "VM")
        findings.append(_vm_finding(severity, vm, signal, cause, action))
    ranked = _ranked(findings, "vm")
    statuses: dict[str, int] = {}
    for v in vms:
        statuses[v["status"] or "unreported"] = statuses.get(v["status"] or "unreported", 0) + 1
    return {
        "findings": ranked,
        "vmsEvaluated": len(vms),
        "vmStatusCounts": statuses,
        "vmsTruncated": vms_truncated,
        **facts,
        "healthy": _healthy(ranked),
    }
