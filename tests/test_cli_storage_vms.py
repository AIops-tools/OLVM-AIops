"""CLI storage and VM commands against live-captured payloads."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from olvm_aiops.cli import app

pytestmark = pytest.mark.unit
runner = CliRunner()
RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


@pytest.fixture
def wired(monkeypatch):
    glob = run("storagedomains_global")
    nfs = next(s for s in glob["storage_domain"] if s["name"] == "lab-nfs-data")
    dc_id = nfs["data_centers"]["data_center"][0]["id"]
    vm = run("vm_after")
    routes = {"/storagedomains": glob,
              f"/datacenters/{dc_id}/storagedomains": run("datacenter_storagedomains"),
              "/vms": {"vm": [vm]}, f"/vms/{vm['id']}/statistics": run("vm_statistics"),
              "/events": {}}
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: routes[path]
    from mcp_server.tools import storage_vms

    monkeypatch.setattr(storage_vms, "_get_connection", lambda target=None: conn)
    return vm


def test_storage_list_shows_the_data_center_status(wired):
    r = runner.invoke(app, ["storage", "list"])
    assert r.exit_code == 0, r.output
    assert "lab-nfs-data" in r.stdout and "active" in r.stdout


def test_storage_capacity_reports_the_live_lab_as_clean(wired):
    r = runner.invoke(app, ["storage", "capacity"])
    assert r.exit_code == 0, r.output
    assert "1 attached domain(s) evaluated, 1 unattached skipped." in r.stdout
    assert "No findings." in r.stdout


def test_vm_health_and_stats(wired):
    r = runner.invoke(app, ["vm", "health"])
    assert r.exit_code == 0, r.output
    assert "1 VM(s): up=1" in " ".join(r.stdout.split())
    s = runner.invoke(app, ["vm", "stats", wired["id"]])
    assert s.exit_code == 0 and "memory.installed" in s.stdout


def test_vm_list_json(wired):
    r = runner.invoke(app, ["vm", "list", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["vms"][0]["status"] == "up"


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """Fixture events are from 2026-09-15 02:xx UTC. Pin "now" so the 24 h event window
    does not turn these tests into a time bomb that fails a day later."""
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)  # 2026-09-15T03:00:00Z
