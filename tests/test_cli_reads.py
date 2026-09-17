"""CLI read commands against live-captured payloads (no engine)."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from olvm_aiops.cli import app

pytestmark = pytest.mark.unit
runner = CliRunner()
FIX = pathlib.Path(__file__).parent / "fixtures"


def load(case: str, name: str) -> dict:
    return json.loads((FIX / case / f"{name}.json").read_text())


def _wire(monkeypatch, routes: dict) -> MagicMock:
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: routes[path]
    from mcp_server.tools import reads

    monkeypatch.setattr(reads, "_get_connection", lambda target=None: conn)
    return conn


def test_host_list_json_is_the_payload(monkeypatch):
    _wire(monkeypatch, {"/hosts": load("olvm-4.5.5-installing", "hosts")})
    r = runner.invoke(app, ["host", "list", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["hosts"][0]["status"] == "installing"


def test_host_health_renders_findings_in_rank_order(monkeypatch):
    case = "olvm-4.5.5-host-rebooting"
    routes = {"/hosts": load(case, "hosts"), "/events": load(case, "events_warning_plus")}
    _wire(monkeypatch, routes)
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "1 host(s): reboot=1" in out
    # low (the PM alert) ranks ahead of info (the in-progress reboot); both labels must survive
    # rich markup parsing.
    assert "1. [low] olvm-kvm1: event 9000" in out
    assert "2. [info] olvm-kvm1: status=reboot" in out
    assert "fencing" in out


def test_event_list_table_and_bad_severity_exits_1(monkeypatch):
    _wire(monkeypatch, {"/events": load("olvm-4.5.5-installing", "events")})
    r = runner.invoke(app, ["event", "list", "--limit", "3"])
    assert r.exit_code == 0, r.output
    assert "Events" in r.stdout
    bad = runner.invoke(app, ["event", "list", "--min-severity", "critical"])
    assert bad.exit_code == 1 and "min_severity" in bad.stdout


def test_job_list_status_filter(monkeypatch):
    _wire(monkeypatch, {"/jobs": load("olvm-4.5.5-host-rebooting", "jobs")})
    r = runner.invoke(app, ["job", "list", "--status", "started", "--json"])
    assert r.exit_code == 0, r.output
    assert {j["status"] for j in json.loads(r.stdout)["jobs"]} == {"started"}


def test_datacenter_and_cluster_lists(monkeypatch):
    _wire(monkeypatch, {"/datacenters": load("olvm-4.5.5-installing", "datacenters"),
                        "/clusters": load("olvm-4.5.5-installing", "clusters")})
    assert "uninitialized" in runner.invoke(app, ["datacenter", "list"]).stdout
    assert "4.7" in runner.invoke(app, ["cluster", "list"]).stdout


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """Fixture events are from 2026-09-15 02:xx UTC. Pin "now" so the 24 h event window
    does not turn these tests into a time bomb that fails a day later."""
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)  # 2026-09-15T03:00:00Z


def test_host_health_prints_vm_candidates_as_candidates(monkeypatch):
    """A guest-agent event names no VM: the CLI must not print a VM as the affected one."""
    events = {"event": [{"index": "9", "code": "10802", "severity": "error",
                         "time": 1789441200 * 1000 - 600_000,
                         "description": "VDSM olvm-kvm1 command VmLogonVDS failed: "
                                        "Guest agent non-responsive",
                         "host": {"id": "h1", "name": "olvm-kvm1"}}]}
    vms = {"vm": [{"id": "v1", "name": "app01", "status": "up", "host": {"id": "h1"}},
                  {"id": "v2", "name": "db01", "status": "up", "host": {"id": "h1"}}]}
    hosts = {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]}
    _wire(monkeypatch, {"/hosts": hosts, "/events": events, "/vms": vms})
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "1. [low] olvm-kvm1: event 10802" in out
    assert "VM candidates (not confirmed): app01, db01" in out
    assert "No findings" not in out


def test_host_health_says_when_the_candidate_vms_could_not_be_read(monkeypatch):
    """An account that cannot read VMs must not make the host look like it runs none."""
    from olvm_aiops.connection import OlvmApiError

    events = {"event": [{"index": "9", "code": "10802", "severity": "error",
                         "time": 1789441200 * 1000 - 600_000,
                         "description": "VDSM olvm-kvm1 command VmLogonVDS failed: "
                                        "Guest agent non-responsive",
                         "host": {"id": "h1", "name": "olvm-kvm1"}}]}
    routes = {"/hosts": {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]},
              "/events": events}
    conn = _wire(monkeypatch, routes)

    def get(path, params=None):
        if path == "/vms":
            raise OlvmApiError("403 Forbidden: query execution failed", status_code=403)
        return routes[path]

    conn.get.side_effect = get
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "VM candidates: not readable" in out and "403" in out
    assert "none on this host" not in out


def test_host_health_says_when_the_candidate_scan_was_cut(monkeypatch):
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.u, "ANALYSIS_LIST_LIMIT", 2)
    events = {"event": [{"index": "9", "code": "10802", "severity": "error",
                         "time": 1789441200 * 1000 - 600_000,
                         "description": "VDSM olvm-kvm1 command VmLogonVDS failed: "
                                        "Guest agent non-responsive",
                         "host": {"id": "h1", "name": "olvm-kvm1"}}]}
    vms = {"vm": [{"id": f"v{i}", "name": f"app0{i}", "status": "up", "host": {"id": "h1"}}
                  for i in range(3)]}
    _wire(monkeypatch, {"/hosts": {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]},
                        "/events": events, "/vms": vms})
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "PARTIAL: the VM scan was cut short" in out


def test_host_health_says_how_many_candidates_it_did_not_print(monkeypatch):
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg, "VM_CANDIDATE_LIMIT", 2)
    events = {"event": [{"index": "9", "code": "10802", "severity": "error",
                         "time": 1789441200 * 1000 - 600_000,
                         "description": "VDSM olvm-kvm1 command VmLogonVDS failed: "
                                        "Guest agent non-responsive",
                         "host": {"id": "h1", "name": "olvm-kvm1"}}]}
    vms = {"vm": [{"id": f"v{i}", "name": f"app0{i}", "status": "up", "host": {"id": "h1"}}
                  for i in range(5)]}
    _wire(monkeypatch, {"/hosts": {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]},
                        "/events": events, "/vms": vms})
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "VM candidates (not confirmed): app00, app01 (+3 more)" in out


def test_host_health_prints_the_related_vm_event_as_a_candidate(monkeypatch):
    """The wrapper names no VM. The CLI must show the VM-level failure the engine logged
    beside it — and must not present it as the confirmed subject of the host finding."""
    now_ms = 1789441200 * 1000
    events = {"event": [
        {"index": "9", "code": "10802", "severity": "error", "time": now_ms - 600_000,
         "description": "VDSM olvm-kvm1 command UpdateVmInterfaceVDS failed: cannot modify MTU",
         "host": {"id": "h1", "name": "olvm-kvm1"}},
        {"index": "10", "code": "935", "severity": "error", "time": now_ms - 600_000,
         "description": "Failed to update Interface nic1 (VirtIO) for VM app01.",
         "vm": {"id": "v1", "name": "app01"}, "host": {"id": "h1", "name": "olvm-kvm1"}},
    ]}
    hosts = {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]}
    _wire(monkeypatch, {"/hosts": hosts, "/events": events, "/vms": {"vm": []}})
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "UpdateVmInterfaceVDS" in out
    assert "Related VM events (not confirmed): app01" in out and "event 935" in out


def test_host_health_says_when_no_vm_event_was_logged_beside_the_wrapper(monkeypatch):
    """Silence would read as "not looked at". The measurement is stated either way."""
    events = {"event": [{"index": "9", "code": "10802", "severity": "error",
                         "time": 1789441200 * 1000 - 600_000,
                         "description": "VDSM olvm-kvm1 command SpmStatusVDS failed: "
                                        "Connection refused",
                         "host": {"id": "h1", "name": "olvm-kvm1"}}]}
    hosts = {"host": [{"id": "h1", "name": "olvm-kvm1", "status": "up"}]}
    _wire(monkeypatch, {"/hosts": hosts, "/events": events, "/vms": {"vm": []}})
    r = runner.invoke(app, ["host", "health"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.stdout.split())
    assert "Related VM events (not confirmed): none logged within 60s" in out
