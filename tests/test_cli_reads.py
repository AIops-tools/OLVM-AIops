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
