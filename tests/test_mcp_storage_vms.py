"""Storage/VM MCP tools delegate to ops and are audited."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from unittest.mock import MagicMock

import pytest

import olvm_aiops.governance.audit as audit_mod
import olvm_aiops.governance.undo as undo_mod
from mcp_server.tools import storage_vms as tools

pytestmark = pytest.mark.unit

RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def gov_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path))
    audit_mod.reset_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    undo_mod.reset_undo_store()


@pytest.fixture
def wired(monkeypatch):
    glob = run("storagedomains_global")
    nfs = next(s for s in glob["storage_domain"] if s["name"] == "lab-nfs-data")
    dc_id = nfs["data_centers"]["data_center"][0]["id"]
    vm = run("vm_after")
    routes = {"/storagedomains": glob,
              f"/datacenters/{dc_id}/storagedomains": run("datacenter_storagedomains"),
              f"/storagedomains/{nfs['id']}": run("storagedomain_get"),
              "/vms": run("vms"), f"/vms/{vm['id']}": vm,
              f"/vms/{vm['id']}/statistics": run("vm_statistics")}
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: routes[path]
    monkeypatch.setattr(tools, "_get_connection", lambda target=None: conn)
    return {"nfs_id": nfs["id"], "vm_id": vm["id"]}


def test_tools_reach_their_ops(wired):
    assert any(d["status"] == "active" for d in tools.storage_domain_list()["storageDomains"])
    assert tools.storage_domain_get(domain_id=wired["nfs_id"])["name"] == "lab-nfs-data"
    assert tools.vm_list()["vms"][0]["name"] == "lab-vm1"
    assert tools.vm_get(vm_id=wired["vm_id"])["status"] == "up"
    assert "memory.installed" in tools.vm_stats(vm_id=wired["vm_id"])["statistics"]
    assert tools.storage_capacity_rca()["healthy"] is True


def test_missing_id_is_the_error_envelope(wired):
    assert "required" in tools.vm_get(vm_id=" ")["error"]


def test_storage_tool_calls_are_audited(wired, gov_home):
    tools.storage_capacity_rca()
    conn = sqlite3.connect(gov_home / "audit.db")
    try:
        tools_called = [r[0] for r in conn.execute("SELECT tool FROM audit_log")]
    finally:
        conn.close()
    assert "storage_capacity_rca" in tools_called


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """Fixture events are from 2026-09-15 02:xx UTC. Pin "now" so the 24 h event window
    does not turn these tests into a time bomb that fails a day later."""
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)  # 2026-09-15T03:00:00Z
