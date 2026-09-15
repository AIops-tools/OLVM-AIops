"""host_health_rca against states captured on a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import diagnose as dg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """The fixtures' events are from 2026-09-15 02:xx UTC. Pin "now" so a 24 h event
    window does not turn these tests into a time bomb that fails a day later."""
    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)

FIX = pathlib.Path(__file__).parent / "fixtures"


def load(case: str, name: str) -> dict:
    return json.loads((FIX / case / f"{name}.json").read_text())


def _conn(hosts: dict, events: dict) -> MagicMock:
    conn = MagicMock()

    def get(path, params=None):
        return {"/hosts": hosts, "/events": events}[path]
    conn.get.side_effect = get
    return conn


def _host(status: str, **extra) -> dict:
    base = load("olvm-4.5.5-installing", "hosts")["host"][0]
    return {**base, "status": status, **extra}


def test_live_reboot_window_is_in_progress_and_pm_alert_is_informational():
    """The captured state: host 'reboot' plus alert 9000. Neither is a failure."""
    case = "olvm-4.5.5-host-rebooting"
    out = dg.host_health_rca(_conn(load(case, "hosts"), load(case, "events_warning_plus")))
    severities = [f["severity"] for f in out["findings"]]
    assert "high" not in severities and "critical" not in severities
    status_f = next(f for f in out["findings"] if f["signal"].startswith("status="))
    assert status_f["severity"] == "info" and "not failed" in status_f["cause"]
    pm = next(f for f in out["findings"] if "9000" in f["signal"])
    assert pm["severity"] == "low" and "fencing" in pm["cause"]
    assert out["healthy"] is True
    assert out["hostStatusCounts"] == {"reboot": 1}


@pytest.mark.parametrize("status", sorted(dg.BROKEN))
def test_broken_states_are_high_and_explain_themselves(status):
    host = _host(status, status_detail="storage_domain_unreachable")
    out = dg.host_health_rca(_conn({"host": [host]}, {}))
    [f] = out["findings"]
    assert f["severity"] == "high" and f["cause"] == dg.BROKEN[status]
    assert "storage_domain_unreachable" in f["signal"]
    assert out["healthy"] is False


def test_findings_are_ranked_worst_first():
    hosts = {"host": [
        _host("up", name="h-upd", id="u1", update_available="true"),
        _host("non_responsive", name="h-dead", id="d1"),
        _host("up", name="h-reinst", id="r1", reinstallation_required="true"),
    ]}
    out = dg.host_health_rca(_conn(hosts, {}))
    assert [(f["rank"], f["host"], f["severity"]) for f in out["findings"]] == [
        (1, "h-dead", "high"), (2, "h-reinst", "medium"), (3, "h-upd", "low")]


def test_a_healthy_up_host_yields_no_findings():
    out = dg.host_health_rca(_conn({"host": [_host("up")]}, {}))
    assert out["findings"] == [] and out["healthy"] is True


def test_error_events_attach_to_their_host_and_foreign_events_are_ignored():
    host = _host("up", id="h1", name="kvm-a")
    events = {"event": [
        {"index": "5", "time": 1789438559456, "severity": "error", "code": "1234",
         "description": "VDSM kvm-a command failed", "host": {"id": "h1"}},
        {"index": "6", "time": 1789438559456, "severity": "warning", "code": "77",
         "description": "some other host", "host": {"id": "not-in-inventory"}},
    ]}
    out = dg.host_health_rca(_conn({"host": [host]}, events))
    [f] = out["findings"]
    assert f["severity"] == "high" and "1234" in f["signal"] and f["host"] == "kvm-a"


def test_the_event_query_is_the_severity_form_that_works_live():
    conn = _conn({}, {})
    dg.host_health_rca(conn, events_limit=50)
    params = [c.kwargs["params"] for c in conn.get.call_args_list if c.args[0] == "/events"][0]
    assert params == {"max": "51", "search": "severity>normal sortby time desc"}


def _event(index, code, severity, host_id, minutes_ago, **refs):
    return {"index": str(index), "code": str(code), "severity": severity,
            "time": (1789441200 - minutes_ago * 60) * 1000,
            "description": f"event {code}", "host": {"id": host_id}, **refs}


def test_host_events_outside_the_window_are_history_not_findings():
    host = _host("up", id="h1", name="kvm-a")
    events = {"event": [_event(1, 5000, "error", "h1", minutes_ago=60 * 30)]}
    out = dg.host_health_rca(_conn({"host": [host]}, events))
    assert out["findings"] == [] and out["eventsOutsideWindow"] == 1 and out["healthy"] is True


def test_events_about_a_vm_or_storage_domain_are_not_blamed_on_the_executing_host():
    """Live: 'Storage Domain lab-nfs-data was attached…' carries the host that ran it."""
    host = _host("up", id="h1", name="kvm-a")
    events = {"event": [
        _event(1, 996, "error", "h1", 5, storage_domain={"id": "sd1"}),
        _event(2, 119, "error", "h1", 5, vm={"id": "vm1"})]}
    assert dg.host_health_rca(_conn({"host": [host]}, events))["findings"] == []


def test_repeated_host_events_collapse_into_one_finding():
    host = _host("up", id="h1", name="kvm-a")
    events = {"event": [_event(i, 7000, "warning", "h1", i) for i in range(1, 6)]}
    [f] = dg.host_health_rca(_conn({"host": [host]}, events))["findings"]
    assert "×5 in window" in f["signal"] and f["severity"] == "medium"


@pytest.mark.parametrize(("external", "severity"), [("error", "high"), ("warning", "medium")])
def test_host_external_status_is_a_finding(external, severity):
    hosts = {"host": [_host("up", external_status=external)]}
    [f] = dg.host_health_rca(_conn(hosts, {}))["findings"]
    assert f["severity"] == severity and f["signal"] == f"external_status={external}"


@pytest.mark.parametrize("hours", [0, 721, True])
def test_events_window_is_bounded(hours):
    with pytest.raises(ValueError, match="events_window_hours"):
        dg.host_health_rca(_conn({}, {}), events_window_hours=hours)
