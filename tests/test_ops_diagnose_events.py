"""Event evidence in the diagnoses: nothing dropped, recovery supersedes, windows honest.

The fake engine applies the event search the way the live engine does (``type=``
codes, ``severity>normal``, newest first, ``max``), so a diagnosis that relied on the
search being ignored — or applied — is tested against the real behaviour.
"""

from __future__ import annotations

import copy
import re

import pytest

from olvm_aiops.connection import OlvmApiError
from olvm_aiops.ops import diagnose as dg
from olvm_aiops.ops import engine_health as eh

pytestmark = pytest.mark.unit

NOW = 1789441200  # 2026-09-15T03:00:00Z
GIB = 1024**3


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(dg.time, "time", lambda: NOW)


def ev(index, code, severity, minutes_ago, description="event", **refs):
    return {"index": str(index), "code": str(code), "severity": severity,
            "time": (NOW - minutes_ago * 60) * 1000, "description": description,
            **{k: {"id": v, "name": v} for k, v in refs.items()}}


class Engine:
    def __init__(self, events=(), hosts=(), vms=(), domains=None, root=None,
                 health=(200, "DB Up!Welcome to Health Status!"), apply_search=True,
                 datacenters=None, sort=True):
        self.events = list(events)
        self.apply_search = apply_search
        self.sort = sort
        domains = domains if domains is not None else []
        self.routes = {
            "/hosts": {"host": list(hosts)}, "/vms": {"vm": list(vms)},
            "/storagedomains": {"storage_domain": [d for d, _ in domains]},
            "": root if root is not None else {"time": NOW * 1000},
            "/datacenters": {"data_center": datacenters if datacenters is not None
                             else [{"id": "dc1", "name": "dc1", "status": "up"}]},
        }
        for sd, dc_status in domains:
            dc = sd["data_centers"]["data_center"][0]["id"]
            self.routes.setdefault(f"/datacenters/{dc}/storagedomains", {"storage_domain": []})
            self.routes[f"/datacenters/{dc}/storagedomains"]["storage_domain"].append(
                {"id": sd["id"], "status": dc_status})
        self.health = health
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append(path)
        if path != "/events":
            value = self.routes[path]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        rows = list(self.events)
        search = params.get("search", "")
        if self.apply_search:
            codes = set(re.findall(r"type=(\d+)", search))
            if codes:
                rows = [e for e in rows if e["code"] in codes]
            elif "severity>normal" in search:
                rows = [e for e in rows if e["severity"] != "normal"]
        if self.sort:
            rows.sort(key=lambda e: -e["time"])
        return {"event": copy.deepcopy(rows[:int(params.get("max", len(rows)))])}

    def probe(self, path):
        assert path == eh.HEALTH_PATH
        if isinstance(self.health, Exception):
            raise self.health
        return self.health


def host(status="up", hid="h1"):
    return {"id": hid, "name": hid, "status": status}


def vm(status="up", vid="vm1", started_minutes_ago=300):
    return {"id": vid, "name": vid, "status": status,
            "start_time": (NOW - started_minutes_ago * 60) * 1000}


def domain(sid="sd1", free_gib=80, used_gib=20, warning="10", blocker="5"):
    return {"id": sid, "name": sid, "type": "data",
            "data_centers": {"data_center": [{"id": "dc1"}]},
            "available": str(int(free_gib * GIB)), "used": str(int(used_gib * GIB)),
            "committed": "0", "warning_low_space_indicator": warning,
            "critical_space_action_blocker": blocker}


# ─── H7: every warning-or-worse event lands in exactly one diagnosis ────────


def test_every_problem_event_belongs_to_exactly_one_diagnosis():
    events = [
        ev(1, 501, "error", 10, host="h1"),
        ev(2, 502, "error", 10, vm="vm1", host="h1"),
        ev(3, 503, "error", 10, storage_domain="sd1", host="h1", data_center="dc1"),
        ev(4, 504, "error", 10, storage_domain="sd1", vm="vm1"),
        ev(5, 505, "alert", 10),
        ev(6, 506, "warning", 10, cluster="Default"),
    ]
    engine = Engine(events, hosts=[host()], vms=[vm()], domains=[(domain(), "active")])
    outputs = {
        "host": dg.host_health_rca(engine), "vm": dg.vm_health_rca(engine),
        "storage": dg.storage_capacity_rca(engine), "engine": eh.engine_health_rca(engine),
    }
    seen: dict[str, list[str]] = {}
    for name, out in outputs.items():
        for f in out["findings"]:
            m = re.match(r"event (\d+) ", f["signal"])
            if m:
                seen.setdefault(m.group(1), []).append(name)
    assert seen == {"501": ["host"], "502": ["vm"], "503": ["storage"], "504": ["vm"],
                    "505": ["engine"], "506": ["engine"]}


def test_engine_wide_alerts_get_their_own_cause_and_severity():
    events = [ev(1, 9022, "alert", 30, "There is no full backup available"),
              ev(2, 846, "alert", 20, "Engine's certificate has expired"),
              ev(3, 10300, "alert", 10, "Cluster Default failed the HA Reservation check",
                 cluster="Default")]
    out = eh.engine_health_rca(Engine(events))
    by_code = {re.match(r"event (\d+)", f["signal"]).group(1): f for f in out["findings"]}
    assert by_code["846"]["severity"] == "critical" and by_code["846"]["rank"] == 1
    assert "engine-backup" in by_code["9022"]["action"] and by_code["9022"]["severity"] == "medium"
    assert by_code["10300"]["subject"] == "cluster Default"
    assert out["healthy"] is False


def test_host_certificate_alert_is_critical_and_never_superseded():
    events = [ev(1, 877, "alert", 60, "Host h1 certificate has expired", host="h1"),
              ev(2, 13, "normal", 30, "Status of host h1 was set to Up.", host="h1")]
    [f] = dg.host_health_rca(Engine(events, hosts=[host()]))["findings"]
    assert f["severity"] == "critical" and "enroll" in f["action"].lower()


def test_storage_domain_deactivated_by_system_is_reported_on_the_domain():
    events = [ev(1, 970, "warning", 15, "Storage Domain sd1 was deactivated by system",
                 storage_domain="sd1", data_center="dc1")]
    out = dg.storage_capacity_rca(Engine(events, domains=[(domain(), "inactive")]))
    event_finding = next(f for f in out["findings"] if f["signal"].startswith("event 970"))
    assert event_finding["severity"] == "high" and event_finding["storageDomain"] == "sd1"


# ─── H8: recovery supersedes ────────────────────────────────────────────────


def test_a_host_problem_is_superseded_once_the_engine_set_the_host_to_up():
    """Review: a host that recovered from non_responsive stayed 'high' for 24 h."""
    events = [ev(1, 12, "error", 60, "Host h1 is non responsive.", host="h1"),
              ev(2, 13, "normal", 30, "Status of host h1 was set to Up.", host="h1")]
    out = dg.host_health_rca(Engine(events, hosts=[host("up")]))
    [f] = out["findings"]
    assert f["severity"] == "info" and f["cause"].startswith("Superseded")
    assert out["healthy"] is True


@pytest.mark.parametrize(("status", "up_minutes_ago", "text"), [
    ("non_responsive", 30, "Status of host h1 was set to Up."),   # not up now
    ("up", 90, "Status of host h1 was set to Up."),               # recovery BEFORE the problem
    ("up", 30, "Status of host h1 was set to Maintenance."),      # code 13, but not Up
])
def test_a_host_problem_is_not_superseded_without_a_later_recovery(status, up_minutes_ago, text):
    events = [ev(1, 12, "error", 60, "Host h1 is non responsive.", host="h1"),
              ev(2, 13, "normal", up_minutes_ago, text, host="h1")]
    findings = dg.host_health_rca(Engine(events, hosts=[host(status)]))["findings"]
    event_finding = next(f for f in findings if f["signal"].startswith("event 12"))
    assert event_finding["severity"] == "high"


def test_a_vm_paused_on_io_error_is_superseded_when_it_recovered_without_restarting():
    """Review: auto-resume keeps start_time, so only 'started later' never cleared it."""
    events = [ev(1, 145, "error", 60, "VM vm1 has been paused due to storage I/O problem.",
                 vm="vm1"),
              ev(2, 196, "normal", 50, "VM vm1 has recovered from paused back to up.", vm="vm1")]
    out = dg.vm_health_rca(Engine(events, vms=[vm("up", started_minutes_ago=600)]))
    [f] = out["findings"]
    assert f["severity"] == "info" and "back up" in f["cause"] and out["healthy"] is True


def test_superseded_by_a_start_does_not_claim_the_vm_is_up():
    events = [ev(1, 54, "error", 60, "Failed to run VM vm1", vm="vm1")]
    [f] = dg.vm_health_rca(Engine(events, vms=[vm("down", started_minutes_ago=30)]))["findings"]
    assert f["cause"] == "Superseded: the VM was started after this event."


def test_repeated_vm_events_collapse_into_one_finding_quoting_the_latest():
    events = [ev(i, 126, "warning", 60 - i, f"VM vm1 is not responding (#{i}).", vm="vm1")
              for i in range(1, 6)]
    [f] = dg.vm_health_rca(Engine(events, vms=[vm(started_minutes_ago=600)]))["findings"]
    assert "×5 in window" in f["signal"] and "(#5)" in f["signal"]


def test_repeated_host_events_quote_the_latest_whatever_order_they_arrive_in():
    events = [ev(2, 7000, "warning", 50, "oldest", host="h1"),
              ev(1, 7000, "warning", 5, "newest", host="h1")]
    engine = Engine(events, hosts=[host("non_responsive")], sort=False)
    [f] = [f for f in dg.host_health_rca(engine)["findings"] if f["signal"].startswith("event")]
    assert "newest" in f["signal"] and "×2" in f["signal"]


# ─── scan honesty and guards ────────────────────────────────────────────────


def test_events_truncated_only_when_the_window_itself_was_cut():
    """Review: 3 events in the window + 300 old ones reported PARTIAL on every run."""
    old = [ev(i, 600, "warning", 60 * 24 * 3 + i, host="h1") for i in range(300)]
    recent = [ev(1000 + i, 601, "warning", i + 1, host="h1") for i in range(3)]
    out = dg.host_health_rca(Engine(old + recent, hosts=[host()]), events_limit=200)
    assert out["eventsTruncated"] is False and out["eventsEvaluated"] == 3
    busy = [ev(i, 602, "warning", 1, host="h1") for i in range(250)]
    assert dg.host_health_rca(Engine(busy, hosts=[host()]), events_limit=200)["eventsTruncated"]


def test_a_normal_event_is_never_a_finding_even_if_the_search_was_not_applied():
    events = [ev(1, 42, "normal", 5, "Host h1 was added.", host="h1")]
    engine = Engine(events, hosts=[host()], apply_search=False)
    assert dg.host_health_rca(engine)["findings"] == []


def test_a_bad_events_limit_names_the_right_parameter():
    with pytest.raises(ValueError, match="events_limit"):
        dg.vm_health_rca(Engine(), events_limit=0)


def test_low_space_threshold_uses_the_exact_ratio_not_the_rounded_one():
    """9.96 % free rounds to 10.0 and used to slip past a 10 % warning."""
    sd = domain(free_gib=9.96, used_gib=90.04, warning="10", blocker="0")
    out = dg.storage_capacity_rca(Engine(domains=[(sd, "active")]))
    [f] = out["findings"]
    assert f["severity"] == "medium" and f["signal"].startswith("free 9.96%")


def test_a_timed_out_data_center_view_says_retry_not_permissions():
    engine = Engine(domains=[(domain(), "active")])
    engine.routes["/datacenters/dc1/storagedomains"] = OlvmApiError(
        "GET timed out after 30s \x1b[31mred\x1b[0m", timed_out=True)
    out = dg.storage_capacity_rca(engine)
    [f] = out["findings"]
    assert "timed out" in f["signal"] and "timeout" in f["action"]
    [error] = out["statusErrors"]
    assert error["timedOut"] is True and "\x1b" not in error["error"]


# ─── engine health ──────────────────────────────────────────────────────────


def test_a_healthy_engine_reports_version_skew_and_counts():
    root = {"time": NOW * 1000 + 1500,
            "product_info": {"version": {"full_version": "4.5.5-1.73.el9"}},
            "summary": {"hosts": {"active": "1", "total": "2"}}}
    out = eh.engine_health_rca(Engine(root=root))
    assert out["healthy"] is True and out["findings"] == []
    assert out["engineVersion"] == "4.5.5-1.73.el9" and out["clockSkewSeconds"] == 1.5
    assert out["summary"]["hosts"] == {"active": 1, "total": 2}
    assert out["summary"]["vms"] == {"active": None, "total": None}


def test_a_health_check_without_db_up_is_high():
    out = eh.engine_health_rca(Engine(health=(503, "DB Down")))
    [f] = out["findings"]
    assert f["severity"] == "high" and "HTTP 503" in f["signal"]


def test_an_unreadable_health_check_is_reported_not_raised():
    out = eh.engine_health_rca(Engine(health=OlvmApiError("Transport error")))
    [f] = out["findings"]
    assert f["severity"] == "medium" and out["healthServlet"]["error"] == "Transport error"


def test_a_large_clock_skew_is_medium_and_a_missing_clock_is_null():
    [f] = eh.engine_health_rca(Engine(root={"time": (NOW - 900) * 1000}))["findings"]
    assert f["severity"] == "medium" and "-900 s" in f["signal"]
    out = eh.engine_health_rca(Engine(root={}))
    assert out["clockSkewSeconds"] is None and out["findings"] == []


def test_a_data_center_status_warning_is_superseded_once_the_data_center_is_up():
    """Live: 986 "Data Center is being initialized" stayed a medium finding (healthy: false)
    long after the data center came up."""
    events = [ev(1, 986, "warning", 30, "Data Center is being initialized, please wait for "
                                        "initialization to complete.", data_center="dc1")]
    out = eh.engine_health_rca(Engine(events))
    [f] = out["findings"]
    assert f["severity"] == "info" and f["cause"].startswith("Superseded") and out["healthy"]


def test_a_data_center_status_alert_counts_while_the_data_center_is_not_up():
    events = [ev(1, 10811, "alert", 5, "Data Center dc1 status was changed to Non Responsive",
                 data_center="dc1")]
    engine = Engine(events, datacenters=[{"id": "dc1", "name": "dc1", "status": "not_operational"}])
    [f] = eh.engine_health_rca(engine)["findings"]
    assert f["severity"] == "high" and "Storage Pool Manager" in f["cause"]


# ─── production feedback (#1): a guest call failing is not a host fault ─────


def vdsm_failure(index, command, message, minutes_ago=10, hid="h1"):
    """AuditLogType.VDS_BROKER_COMMAND_FAILURE: "VDSM ${VdsName} command ${CommandName}
    failed: ${message}" — a wrapper whose real subject is the command that failed."""
    return ev(index, dg.VDSM_COMMAND_FAILURE, "error", minutes_ago,
              f"VDSM {hid} command {command} failed: {message}", host=hid)


def on_host(vid, hid="h1", status="up"):
    return {**vm(status, vid), "host": {"id": hid, "name": hid}}


def test_a_guest_agent_command_failure_is_not_reported_as_a_host_fault():
    """Production feedback: 10802 "VmLogonVDS failed: Guest agent non-responsive" ranked
    two hosts high while both were up, ok, and needed nothing."""
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    out = dg.host_health_rca(engine)
    [f] = out["findings"]
    assert f["severity"] == "low" and out["healthy"] is True
    assert "guest agent" in f["cause"] and "not a fault of the host" in f["cause"]
    assert f["vmCandidates"]["vms"] == [{"id": "vm1", "name": "vm1", "status": "up"}]
    assert f["vmCandidates"]["returned"] == 1 and f["vmCandidates"]["total"] == 1
    assert f["vmCandidates"]["truncated"] is False and f["vmCandidates"]["error"] is None


def test_vm_candidates_are_the_vms_on_that_host_only_and_are_capped():
    vms = [on_host(f"vm{i}") for i in range(dg.VM_CANDIDATE_LIMIT + 3)]
    vms.append(on_host("elsewhere", "h2"))
    engine = Engine([vdsm_failure(1, "VmLogoffVDS", "Guest agent non-responsive")],
                    hosts=[host("up", "h1"), host("up", "h2")], vms=vms)
    [f] = [f for f in dg.host_health_rca(engine)["findings"] if "vmCandidates" in f]
    candidates = f["vmCandidates"]
    assert candidates["returned"] == dg.VM_CANDIDATE_LIMIT == candidates["limit"]
    assert candidates["total"] == dg.VM_CANDIDATE_LIMIT + 3 and candidates["truncated"] is True
    assert all(c["id"] != "elsewhere" for c in candidates["vms"])


def test_a_host_running_nothing_gets_an_empty_candidate_list_not_a_guess():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=[on_host("vm1", "h2")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["vmCandidates"]["vms"] == [] and f["vmCandidates"]["total"] == 0
    assert f["vmCandidates"]["error"] is None


def test_unreadable_vms_say_so_instead_of_looking_like_no_candidates():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")])
    engine.routes["/vms"] = OlvmApiError("403 Forbidden: the account cannot read VMs")
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["vmCandidates"]["vms"] == [] and f["vmCandidates"]["total"] is None
    assert "403" in f["vmCandidates"]["error"]


def test_the_candidate_vms_are_read_once_for_the_whole_diagnosis():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive", hid="h1"),
                     vdsm_failure(2, "VmLogonVDS", "Guest agent non-responsive", hid="h2")],
                    hosts=[host("up", "h1"), host("up", "h2")], vms=[on_host("vm1")])
    out = dg.host_health_rca(engine)
    assert len([f for f in out["findings"] if "vmCandidates" in f]) == 2
    assert engine.calls.count("/vms") == 1


def test_a_vdsm_command_failure_that_is_not_a_guest_call_stays_a_host_finding():
    engine = Engine([vdsm_failure(1, "SpmStatusVDS", "Connection timeout")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["severity"] == "high" and "vmCandidates" not in f


def test_a_guest_agent_failure_is_not_superseded_by_a_later_host_recovery():
    events = [vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive", minutes_ago=60),
              ev(2, 13, "normal", 30, "Status of host h1 was set to Up.", host="h1")]
    [f] = dg.host_health_rca(Engine(events, hosts=[host("up")]))["findings"]
    assert f["severity"] == "low" and "guest agent" in f["cause"]


def test_only_a_vdsm_command_failure_is_read_as_a_guest_call():
    """Other events quote failed commands too; the guest rule keys on code 10802."""
    engine = Engine([ev(1, 519, "error", 10,
                        "VDSM h1 command VmLogonVDS failed: Guest agent non-responsive",
                        host="h1")], hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["severity"] == "high" and "vmCandidates" not in f


def test_a_cut_vm_scan_says_the_candidate_list_is_a_lower_bound(monkeypatch):
    """A candidate list read from a truncated scan must not look complete."""
    monkeypatch.setattr(dg.u, "ANALYSIS_LIST_LIMIT", 2)
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=[on_host(f"vm{i}") for i in range(3)])
    [f] = [f for f in dg.host_health_rca(engine)["findings"] if "vmCandidates" in f]
    assert f["vmCandidates"]["scanTruncated"] is True
    assert f["vmCandidates"]["total"] == 2  # a lower bound: the scan stopped at the limit


def test_a_complete_vm_scan_is_not_flagged_as_cut():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["vmCandidates"]["scanTruncated"] is False


def test_a_real_vdsm_failure_is_not_collapsed_into_a_newer_guest_agent_one():
    """Review: 10802 wraps every vdsm command, so grouping by code alone let the newest
    VmLogonVDS classify an SpmStatusVDS failure — and its text vanished from the payload."""
    events = [vdsm_failure(1, "SpmStatusVDS", "Connection refused", minutes_ago=60),
              vdsm_failure(2, "VmLogonVDS", "Guest agent non-responsive", minutes_ago=5)]
    out = dg.host_health_rca(Engine(events, hosts=[host("up")], vms=[on_host("vm1")]))
    by_command = {f["signal"].split("command ")[1].split(" ")[0]: f for f in out["findings"]}
    assert by_command["SpmStatusVDS"]["severity"] == "high"
    assert by_command["VmLogonVDS"]["severity"] == "low"
    assert out["healthy"] is False  # the real vdsm failure still decides this


def test_a_guest_verb_that_failed_for_another_reason_stays_a_host_finding():
    """Review: the command alone said "not a fault of the host" — but the same verb fails
    with a vdsm transport error, which is exactly a fault of the host's link to vdsm."""
    engine = Engine([vdsm_failure(1, "VmLogonVDS",
                                  "Message timeout which can be caused by communication issues")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["severity"] == "high" and "vmCandidates" not in f


def test_no_guest_finding_means_the_vm_list_is_never_read():
    engine = Engine([ev(1, 12, "error", 10, "Host h1 is non responsive.", host="h1")],
                    hosts=[host("non_responsive")], vms=[on_host("vm1")])
    dg.host_health_rca(engine)
    assert engine.calls.count("/vms") == 0


def test_a_read_failure_without_a_message_still_explains_itself():
    """`error` must never be an empty string: anything testing it for truth would read that
    as "no error" and `total: 0` as "this host runs nothing"."""
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")])
    engine.routes["/vms"] = OlvmApiError("")
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["vmCandidates"]["error"] == "OlvmApiError without a message"
    assert f["vmCandidates"]["total"] is None


def test_events_of_one_code_stay_one_finding_even_when_they_quote_commands():
    """Only the vdsm wrapper code splits by command; other codes collapse per code as before."""
    events = [ev(i, 519, "error", 60 - i * 10,
                 f"Host installation failed: command Step{i}Cmd failed: not found", host="h1")
              for i in (1, 2)]
    [f] = dg.host_health_rca(Engine(events, hosts=[host("up")]))["findings"]
    assert "(×2 in window)" in f["signal"] and f["severity"] == "high"


def test_two_conditions_of_one_command_do_not_share_a_finding():
    """Second review: splitting 10802 by command still merged "guest agent non-responsive"
    with "Message timeout" — the same command reports two different conditions, and the
    newest member classified both."""
    events = [vdsm_failure(1, "VmLogonVDS", "Message timeout which can be caused by "
                           "communication issues", minutes_ago=60),
              vdsm_failure(2, "VmLogonVDS", "Message timeout which can be caused by "
                           "communication issues", minutes_ago=50),
              vdsm_failure(3, "VmLogonVDS", "Guest agent non-responsive", minutes_ago=5)]
    out = dg.host_health_rca(Engine(events, hosts=[host("up")], vms=[on_host("vm1")]))
    by_severity = {f["severity"]: f for f in out["findings"]}
    assert set(by_severity) == {"high", "low"}
    high = by_severity["high"]["signal"]
    assert "Message timeout" in high and "×2" in high
    assert "Guest agent" in by_severity["low"]["signal"] and "×" not in by_severity["low"]["signal"]
    assert out["healthy"] is False


def test_a_guest_agent_message_under_another_command_is_still_a_host_finding():
    """Both halves are required: the command says the call went into a VM."""
    engine = Engine([vdsm_failure(1, "VmLockVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["severity"] == "high" and "vmCandidates" not in f


def test_the_hyphenated_spelling_of_the_guest_agent_message_also_matches():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest-agent non-responsive")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    [f] = dg.host_health_rca(engine)["findings"]
    assert f["severity"] == "low" and f["vmCandidates"]["total"] == 1


def test_a_failed_read_reports_no_truncation_it_did_not_measure():
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")])
    engine.routes["/vms"] = OlvmApiError("403 Forbidden")
    [f] = dg.host_health_rca(engine)["findings"]
    c = f["vmCandidates"]
    assert c["total"] is None and c["truncated"] is None and c["scanTruncated"] is None


def test_candidates_are_listed_in_a_stable_order():
    vms = [on_host(n) for n in ("charlie", "alpha", "bravo")]
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive")],
                    hosts=[host("up")], vms=vms)
    [f] = dg.host_health_rca(engine)["findings"]
    assert [v["name"] for v in f["vmCandidates"]["vms"]] == ["alpha", "bravo", "charlie"]


# ─── production feedback (#1, ReadOnlyAdmin re-run): the wrapper's subject is the ──
# ─── command, and the engine logs the operation's own failure beside it ───────────


#: AuditLogType.NETWORK_UPDATE_VM_INTERFACE_FAILED — the engine's own failure event for
#: the operation an UpdateVmInterfaceVDS wrapper is the low-level half of. The rule under
#: test keys on "names a VM", not on this code: every VM-level failure pairs the same way.
NIC_UPDATE_FAILED = 935


def nic_update_failed(index, vid="vm1", minutes_ago=10, hid="h1"):
    """AuditLogType.NETWORK_UPDATE_VM_INTERFACE_FAILED(935, ERROR): "Failed to update
    Interface ${InterfaceName} (${InterfaceType}) for VM ${VmName}." — the engine's own
    failure event for the operation the vdsm wrapper is the low-level half of."""
    refs = {"vm": vid, **({"host": hid} if hid else {})}
    return ev(index, NIC_UPDATE_FAILED, "error", minutes_ago,
              f"Failed to update Interface nic1 (VirtIO) for VM {vid}. (User: admin)", **refs)


def wrapper_finding(out):
    [f] = [f for f in out["findings"] if "relatedVmEvents" in f]
    return f


def test_a_vdsm_wrapper_failure_names_the_command_instead_of_blaming_the_host():
    """ReadOnlyAdmin re-run: `UpdateVmInterfaceVDS failed: cannot modify MTU` was ranked
    high under the catch-all "The engine logged a problem for this host" — but the engine's
    own template makes ${CommandName} the subject and logs it against the host only because
    that is where the call ran. Fixing the guest-agent case left every other command in the
    catch-all; this is the same defect, one instance further out."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    f = wrapper_finding(dg.host_health_rca(engine))
    assert "UpdateVmInterfaceVDS" in f["cause"]
    assert "problem for this host" not in f["cause"]


def test_a_vdsm_wrapper_failure_is_not_downgraded_without_evidence():
    """"cannot modify MTU" may well be the host's own network. The command name alone
    cannot say the host is fine, and this line does not downgrade on a guess."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    out = dg.host_health_rca(engine)
    assert wrapper_finding(out)["severity"] == "high" and out["healthy"] is False


def test_a_wrapper_finding_offers_the_vm_failure_the_engine_logged_beside_it():
    """Live: the Portal shows a host-side `UpdateVmInterfaceVDS` failure and a VM-side
    `nic1` failure at the same second, and treats them as one incident. Two diagnoses
    reported them independently with nothing linking them; the wrapper carries no VM
    reference, so the pairing is offered as a candidate."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU"),
                     nic_update_failed(2)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    related = wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]
    assert related["returned"] == 1 and related["truncated"] is False
    [row] = related["events"]
    assert row["vmId"] == "vm1" and row["code"] == NIC_UPDATE_FAILED
    assert row["secondsApart"] == 0 and "nic1" in row["description"]
    assert related["windowSeconds"] == dg.RELATED_EVENT_WINDOW_SEC


def test_the_related_list_says_it_is_a_candidate_not_a_mapping():
    """A model reading one candidate must not name it as the affected VM — the same rule
    vmCandidates already carries, for the same reason."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU"),
                     nic_update_failed(2)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    f = wrapper_finding(dg.host_health_rca(engine))
    assert "candidate" in f["action"].lower() and "not" in f["action"].lower()


def test_a_vm_event_on_another_host_is_not_offered_as_related():
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU", hid="h1"),
                     nic_update_failed(2, hid="h2")],
                    hosts=[host("up", "h1")], vms=[on_host("vm1")])
    assert wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]["events"] == []


def test_a_vm_event_with_no_host_reference_is_still_offered():
    """A missing reference is not a contradicting one: dropping it would make "the engine
    did not say which host" look like "it said another host"."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU"),
                     nic_update_failed(2, hid=None)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    assert wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]["returned"] == 1


def test_a_vm_event_far_from_the_wrapper_is_not_offered_as_related():
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU",
                                  minutes_ago=10),
                     nic_update_failed(2, minutes_ago=180)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    assert wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]["events"] == []


def test_the_related_list_is_capped_and_says_so():
    events = [vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU")]
    events += [nic_update_failed(i + 2, vid=f"vm{i}") for i in range(dg.RELATED_EVENT_LIMIT + 3)]
    engine = Engine(events, hosts=[host("up")], vms=[on_host("vm1")])
    related = wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]
    assert related["returned"] == dg.RELATED_EVENT_LIMIT and related["truncated"] is True


def test_a_wrapper_with_no_vm_failure_beside_it_says_so_explicitly():
    """An empty list is a measurement: the engine logged no VM-level failure near it.
    Omitting the field would leave "nothing was found" and "nothing was looked for" the
    same shape."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU")],
                    hosts=[host("up")], vms=[on_host("vm1")])
    related = wrapper_finding(dg.host_health_rca(engine))["relatedVmEvents"]
    assert related["events"] == [] and related["returned"] == 0
    assert related["truncated"] is False


def test_a_host_finding_that_is_not_a_wrapper_carries_no_related_list():
    engine = Engine([ev(1, 12, "error", 10, "Host h1 is non responsive.", host="h1")],
                    hosts=[host("non_responsive")], vms=[on_host("vm1")])
    assert all("relatedVmEvents" not in f for f in dg.host_health_rca(engine)["findings"])


def test_a_guest_agent_finding_carries_both_candidate_fields():
    """They answer different questions: which VMs run here at all, and which VM-level
    failure the engine logged beside this one."""
    engine = Engine([vdsm_failure(1, "VmLogonVDS", "Guest agent non-responsive"),
                     nic_update_failed(2)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    f = wrapper_finding(dg.host_health_rca(engine))
    assert f["severity"] == "low"
    assert f["vmCandidates"]["total"] == 1 and f["relatedVmEvents"]["returned"] == 1


def test_a_wrapper_that_is_not_a_guest_call_still_reads_no_vm_list():
    """The related list comes out of the events already fetched; it must not turn every
    wrapper failure into an extra /vms read."""
    engine = Engine([vdsm_failure(1, "UpdateVmInterfaceVDS", "cannot modify MTU"),
                     nic_update_failed(2)],
                    hosts=[host("up")], vms=[on_host("vm1")])
    dg.host_health_rca(engine)
    assert engine.calls.count("/vms") == 0


# ─── a failed login is an authentication event, not engine ill-health ───────

USER_LOGIN_FAILED = 114            # AuditLogType.USER_VDC_LOGIN_FAILED(114, ERROR)
USER_ACCOUNT_LOCKED = 160          # AuditLogType.USER_ACCOUNT_DISABLED_OR_LOCKED(160, ERROR)


def login_failed(index, minutes_ago=10, who="admin@ovirt"):
    """"User ${UserName} connecting from '${SourceIP}' failed to log in${LoginErrMsg}."
    It names a user and nothing else, so it lands in the engine diagnosis."""
    return ev(index, USER_LOGIN_FAILED, "error", minutes_ago,
              f"User {who} connecting from '10.0.0.9' failed to log in: Cannot login. "
              "User Password Is Invalid.", user=who)


def test_a_mistyped_password_does_not_make_the_engine_unhealthy():
    """ReadOnlyAdmin re-run: setting the account up produced two failed logins, and the
    catch-all ranked them `high` with "The engine logged a problem for this engine" — so
    any engine where anyone mistypes a password reports itself unhealthy for 24 hours."""
    engine = Engine([login_failed(1), login_failed(2, minutes_ago=9)], hosts=[host("up")])
    out = eh.engine_health_rca(engine)
    [f] = out["findings"]
    assert f["severity"] == "low" and out["healthy"] is True
    assert "×2 in window" in f["signal"]


def test_a_failed_login_says_what_it_is():
    engine = Engine([login_failed(1)], hosts=[host("up")])
    [f] = eh.engine_health_rca(engine)["findings"]
    assert "problem for this engine" not in f["cause"]
    assert "brute" in f["cause"].lower() or "repeat" in f["cause"].lower()


def test_the_severity_of_a_failed_login_does_not_move_with_the_count():
    """How many failures are too many is the operator's policy and the engine records no
    threshold, so this diagnosis does not invent one: 1 and 50 report the same severity,
    and the count is in the signal for the reader to judge."""
    one = Engine([login_failed(1)], hosts=[host("up")])
    many = Engine([login_failed(i) for i in range(50)], hosts=[host("up")])
    [f_one] = eh.engine_health_rca(one)["findings"]
    [f_many] = eh.engine_health_rca(many)["findings"]
    assert f_one["severity"] == f_many["severity"] == "low"
    assert "×50 in window" in f_many["signal"]


def test_an_account_that_got_locked_is_still_reported_high():
    """The downgrade above is only safe because the consequential outcome stays high — and
    it is the one that would take this tool's own account offline."""
    events = [ev(1, USER_ACCOUNT_LOCKED, "error", 10,
                 "User svc-aiops cannot login, as it got disabled or locked. "
                 "Please contact the system administrator.", user="svc-aiops")]
    out = eh.engine_health_rca(Engine(events, hosts=[host("up")]))
    [f] = out["findings"]
    assert f["severity"] == "high" and out["healthy"] is False
    assert "locked" in f["cause"].lower() and "problem for this engine" not in f["cause"]


# ─── production feedback (#1, raw rows): 10803 and the manual unlock record ────

IRS_COMMAND_FAILURE = 10803    # AuditLogType.IRS_BROKER_COMMAND_FAILURE(10803, ERROR)
UNLOCK_SCRIPT_RUN = 2024       # AuditLogType.USER_RUN_UNLOCK_ENTITY_SCRIPT


def irs_failure(index, command, message, minutes_ago=10, **refs):
    """"VDSM command ${CommandName} failed: ${message}" — the storage-pool broker's
    wrapper. No host name in the template; live, every structured ref was null."""
    return ev(index, IRS_COMMAND_FAILURE, "error", minutes_ago,
              f"VDSM command {command} failed: {message}", **refs)


def unlock_record(index, minutes_ago=30):
    """unlock_entity.sh INSERTs this row itself: severity 10 (alert), a fixed message, and
    no structured reference at all — the entity is named only in the text."""
    return ev(index, UNLOCK_SCRIPT_RUN, "alert", minutes_ago,
              "/usr/share/ovirt-engine/setup/dbutils/unlock_entity.sh :  System user root "
              "run manually unlock_entity script on entity [type,id] [disk,IMG-1] "
              "with db user engine")


def test_a_storage_broker_failure_names_its_command():
    """Live: `DeleteImageGroupVDS failed: Image does not exist` is 10803, not 10802, and
    fell through to "The engine logged a problem for this engine" — the catch-all 0.4.0
    fixed for 10802 and left for its sibling."""
    engine = Engine([irs_failure(1, "DeleteImageGroupVDS", "Image does not exist in domain")],
                    hosts=[host("up")])
    [f] = eh.engine_health_rca(engine)["findings"]
    assert "DeleteImageGroupVDS" in f["cause"]
    assert "problem for this engine" not in f["cause"]


def test_a_storage_broker_failure_is_not_downgraded():
    """"Image does not exist" after a manual unlock may be a real inconsistency; the
    reporter agreed it merits its own investigation."""
    engine = Engine([irs_failure(1, "DeleteImageGroupVDS", "Image does not exist in domain")],
                    hosts=[host("up")])
    out = eh.engine_health_rca(engine)
    assert out["findings"][0]["severity"] == "high" and out["healthy"] is False


def test_two_storage_broker_commands_are_not_collapsed_into_one_finding():
    """The 0.2.0 lesson for 10802, applied to its sibling: grouping by code lets the newest
    command classify an older, different one and drops its text from the payload."""
    engine = Engine([irs_failure(1, "DeleteImageGroupVDS", "Image does not exist",
                                 minutes_ago=60),
                     irs_failure(2, "SpmStatusVDS", "Connection refused", minutes_ago=5)],
                    hosts=[host("up")])
    causes = " ".join(f["cause"] for f in eh.engine_health_rca(engine)["findings"])
    assert "DeleteImageGroupVDS" in causes and "SpmStatusVDS" in causes


def test_a_storage_broker_failure_names_its_command_wherever_it_lands():
    """If a 10803 row does carry a storage-domain ref, it goes to the storage diagnosis —
    and must not fall back to the catch-all there either."""
    events = [irs_failure(1, "DeleteImageGroupVDS", "Image does not exist",
                          storage_domain="sd1")]
    engine = Engine(events, hosts=[host()], domains=[(domain(), "active")])
    findings = [f for f in dg.storage_capacity_rca(engine)["findings"]
                if f["signal"].startswith("event ")]
    assert len(findings) == 1 and "DeleteImageGroupVDS" in findings[0]["cause"]


def test_a_manual_unlock_is_not_reported_as_an_engine_problem():
    """Live: the script writes its row at alert, so the operator's own fix came back as a
    high "problem for this engine" and kept the engine unhealthy for 24 hours."""
    out = eh.engine_health_rca(Engine([unlock_record(1)], hosts=[host("up")]))
    [f] = out["findings"]
    assert f["severity"] == "low" and out["healthy"] is True
    assert "unlock_entity" in f["cause"] and "problem for this engine" not in f["cause"]


def test_a_manual_unlock_does_not_hide_what_followed_it():
    """The reporter's engine exactly: the unlock came first, the image-not-found failure
    after. The downgrade is only safe because the consequence stays high on its own."""
    out = eh.engine_health_rca(Engine(
        [unlock_record(1, minutes_ago=30),
         irs_failure(2, "DeleteImageGroupVDS", "Image does not exist", minutes_ago=10)],
        hosts=[host("up")]))
    by_code = {int(f["signal"].split()[1]): f["severity"] for f in out["findings"]}
    assert by_code == {UNLOCK_SCRIPT_RUN: "low", IRS_COMMAND_FAILURE: "high"}
    assert out["healthy"] is False
