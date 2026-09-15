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

    def get(self, path, params=None):
        params = params or {}
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
