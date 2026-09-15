"""Every CLI read is audited, and a failed read is never a quiet success.

Each read command calls the MCP tool of the same name (``mcp_server.tools``), so it
runs through ``@governed_tool`` and lands an audit row. Before this, the CLI called
``olvm_aiops.ops`` directly: on a live engine ``olvm-aiops host list`` returned the
host and never created ``audit.db``, while the documentation promised that every
call, MCP or CLI, is audited.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sqlite3
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from olvm_aiops.cli import app

pytestmark = pytest.mark.unit
ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "tests" / "fixtures" / "olvm-4.5.5-running"
runner = CliRunner()

#: CLI command (ids filled in per test) → the MCP tool it must go through.
COMMANDS = [
    (["engine", "health"], "engine_health_rca"),
    (["datacenter", "list"], "datacenter_list"),
    (["cluster", "list"], "cluster_list"),
    (["host", "list"], "host_list"),
    (["host", "get", "{host}"], "host_get"),
    (["host", "health"], "host_health_rca"),
    (["event", "list"], "event_list"),
    (["job", "list"], "job_list"),
    (["storage", "list"], "storage_domain_list"),
    (["storage", "get", "{sd}"], "storage_domain_get"),
    (["storage", "capacity"], "storage_capacity_rca"),
    (["vm", "list"], "vm_list"),
    (["vm", "get", "{vm}"], "vm_get"),
    (["vm", "stats", "{vm}"], "vm_stats"),
    (["vm", "health"], "vm_health_rca"),
]


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """Fixture events are from 2026-09-15 02:xx UTC; pin "now" for the event window."""
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)


def _wire(monkeypatch, get) -> None:
    from mcp_server.tools import reads, storage_vms

    for module in (reads, storage_vms):
        monkeypatch.setattr(module, "_get_connection", get)


@pytest.fixture
def engine(monkeypatch) -> dict:
    glob = run("storagedomains_global")
    nfs = next(s for s in glob["storage_domain"] if s["name"] == "lab-nfs-data")
    dc_id = nfs["data_centers"]["data_center"][0]["id"]
    vm, host = run("vm_after"), run("hosts")["host"][0]
    routes = {
        "/datacenters": run("datacenters"), "/clusters": run("clusters"), "/hosts": run("hosts"),
        f"/hosts/{host['id']}": host, "/events": run("events"), "/jobs": run("jobs"),
        "/storagedomains": glob,
        f"/datacenters/{dc_id}/storagedomains": run("datacenter_storagedomains"),
        f"/storagedomains/{nfs['id']}": run("storagedomain_get"),
        "/vms": run("vms"), f"/vms/{vm['id']}": vm,
        f"/vms/{vm['id']}/statistics": run("vm_statistics"),
    }
    routes[""] = run("api_root")
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: routes[path]
    conn.probe.return_value = (200, "DB Up!Welcome to Health Status!")
    _wire(monkeypatch, lambda target=None: conn)
    return {"host": host["id"], "sd": nfs["id"], "vm": vm["id"]}


def _audited_tools() -> list[str]:
    db = pathlib.Path(os.environ["OLVM_AIOPS_HOME"]) / "audit.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log")]
    finally:
        conn.close()


@pytest.mark.parametrize(("argv", "tool"), COMMANDS, ids=[c[1] for c in COMMANDS])
def test_every_cli_read_lands_an_audit_row(engine, argv, tool):
    args = [a.format(**engine) for a in argv]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert _audited_tools() == [tool]


def test_every_read_tool_has_an_audited_cli_command():
    """A new read tool without a CLI mapping here fails, so the table cannot rot."""
    from mcp_server import server

    reads = {name for name, tool in server.mcp._tool_manager._tools.items()
             if (tool.fn.__doc__ or "").lstrip().startswith("[READ]")} - {"undo_list"}
    assert {tool for _, tool in COMMANDS} == reads


def test_no_cli_module_reaches_the_engine_around_the_harness():
    offenders = []
    for path in sorted((ROOT / "olvm_aiops" / "cli").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module.startswith("olvm_aiops.ops")
                    or (node.module == "olvm_aiops" and any(a.name == "ops" for a in node.names))):
                offenders.append(f"{path.name}:{node.lineno} imports {node.module}")
            if isinstance(node, ast.Name) and node.id in ("ConnectionManager", "OlvmConnection"):
                offenders.append(f"{path.name}:{node.lineno} uses {node.id}")
    assert not offenders, offenders


def test_an_engine_error_is_one_line_exit_1_even_with_markup_in_it(monkeypatch):
    from olvm_aiops.connection import OlvmApiError

    def refuse(target=None):
        raise OlvmApiError("Engine error (500) on /hosts. detail [/bold] from the engine",
                           status_code=500)

    _wire(monkeypatch, refuse)
    result = runner.invoke(app, ["host", "list"])
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "detail [/bold] from the engine" in out and "Traceback" not in out


def test_a_wrong_master_password_is_a_teaching_error_not_a_traceback(monkeypatch):
    from olvm_aiops.secretstore import MasterPasswordError

    def locked(target=None):
        raise MasterPasswordError("Wrong master password (could not decrypt the secret store).")

    _wire(monkeypatch, locked)
    result = runner.invoke(app, ["vm", "list"])
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "Wrong master password" in out and "operation failed" not in out
