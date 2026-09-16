"""Smoke tests for olvm-aiops.

Proves: every module imports, the CLI Typer app builds and --help works, the
MCP server exposes the expected tools, EVERY MCP tool carries the harness
marker ``_is_governed_tool``, and a tool's risk level agrees with its
``[READ]``/``[WRITE]`` docstring tag. No engine is needed.

EXPECTED_TOOLS grows as the ops layer is built from the live-engine probe.
"""

import asyncio
import importlib

import pytest
from typer.testing import CliRunner

EXPECTED_TOOLS = {
    "engine_health_rca",
    "undo_list", "undo_apply",
    "datacenter_list", "cluster_list", "host_list", "host_get",
    "event_list", "job_list", "host_health_rca",
    "storage_domain_list", "storage_domain_get", "vm_list", "vm_get", "vm_stats",
    "storage_capacity_rca", "vm_health_rca",
}


@pytest.mark.unit
def test_all_modules_import():
    for name in (
        "olvm_aiops",
        "olvm_aiops.config",
        "olvm_aiops.connection",
        "olvm_aiops.doctor",
        "olvm_aiops.secretstore",
        "olvm_aiops.ops._util",
        "olvm_aiops.cli",
        "olvm_aiops.cli._root",
        "olvm_aiops.cli._common",
        "olvm_aiops.cli.init",
        "olvm_aiops.cli.secret",
        "olvm_aiops.cli.doctor",
        "olvm_aiops.cli.undo",
        "olvm_aiops.cli.inventory",
        "olvm_aiops.cli.activity",
        "olvm_aiops.cli.storage_vms",
        "mcp_server.server",
        "mcp_server._shared",
        "mcp_server.tools.undo",
        "mcp_server.tools.reads",
        "mcp_server.tools.storage_vms",
        "olvm_aiops.ops.storage",
        "olvm_aiops.ops.vms",
        "olvm_aiops.ops.inventory",
        "olvm_aiops.ops.activity",
        "olvm_aiops.ops.diagnose",
    ):
        importlib.import_module(name)


@pytest.mark.unit
def test_version_matches_pyproject():
    """__version__ is single-sourced from package metadata and must track pyproject."""
    import tomllib
    from pathlib import Path

    import olvm_aiops

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]
    assert olvm_aiops.__version__ == expected


@pytest.mark.unit
def test_cli_app_builds_and_help_works():
    from olvm_aiops.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("secret", "init", "doctor", "undo", "mcp",
                "datacenter", "cluster", "host", "event", "job", "storage", "vm"):
        assert sub in result.output


@pytest.mark.unit
def test_cli_leaf_help_triggers_lazy_imports():
    from olvm_aiops.cli import app

    runner = CliRunner()
    for cmd in (
        ["secret", "--help"], ["secret", "list", "--help"], ["secret", "set", "--help"],
        ["undo", "--help"], ["undo", "list", "--help"], ["undo", "apply", "--help"],
        ["doctor", "--help"], ["init", "--help"],
        ["datacenter", "list", "--help"], ["cluster", "list", "--help"],
        ["host", "list", "--help"], ["host", "get", "--help"],
        ["host", "health", "--help"], ["event", "list", "--help"],
        ["job", "list", "--help"],
        ["storage", "list", "--help"], ["storage", "get", "--help"],
        ["storage", "capacity", "--help"], ["vm", "list", "--help"],
        ["vm", "get", "--help"], ["vm", "stats", "--help"],
        ["vm", "health", "--help"],
    ):
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0, f"{cmd} failed: {result.output}"


@pytest.mark.unit
def test_mcp_list_tools_exposes_expected_tools():
    from mcp_server.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOLS, (
        f"missing: {EXPECTED_TOOLS - names}; unexpected: {names - EXPECTED_TOOLS}")


@pytest.mark.unit
def test_every_mcp_tool_is_governed_by_harness():
    from mcp_server import server

    tool_objs = server.mcp._tool_manager._tools
    assert set(tool_objs) == EXPECTED_TOOLS, "tool registry differs from EXPECTED_TOOLS"
    for name, tool in tool_objs.items():
        fn = getattr(tool, "fn", None)
        assert fn is not None, f"{name} has no fn"
        assert getattr(fn, "_is_governed_tool", False), (
            f"{name} is not wrapped with @governed_tool (harness marker missing)"
        )


@pytest.mark.unit
def test_connection_manager_registers_for_atexit_cleanup():
    import olvm_aiops.connection as conn_mod
    from olvm_aiops.config import AppConfig

    mgr = conn_mod.ConnectionManager(AppConfig(targets=()))
    assert mgr in conn_mod._MANAGERS

    closed = {"n": 0}

    class _Conn:
        def close(self):
            closed["n"] += 1

    mgr._connections["engine1"] = _Conn()
    conn_mod._close_all_managers()
    assert closed["n"] == 1
    assert mgr.list_connected() == []


@pytest.mark.unit
def test_risk_level_agrees_with_read_write_docstring_tag():
    """A tool's risk level must match its [READ]/[WRITE] tag.

    ``risk_level`` decides the audit tier and dry-run / undo handling; the tag
    is what docs and capability tables are built from. Line-wide this caught
    16 writes mislabelled as reads once, so every new tool inherits it.
    """
    from mcp_server import server

    untagged, mismatched = [], []
    for name, tool in server.mcp._tool_manager._tools.items():
        doc = (tool.fn.__doc__ or "").lstrip()
        if doc.startswith("[READ]"):
            tagged_as_read = True
        elif doc.startswith("[WRITE]"):
            tagged_as_read = False
        else:
            untagged.append(name)
            continue
        if tagged_as_read != (getattr(tool.fn, "_risk_level", "low") == "low"):
            mismatched.append(name)

    assert not untagged, f"tools missing a [READ]/[WRITE] docstring tag: {untagged}"
    assert not mismatched, f"risk_level disagrees with the docstring tag: {mismatched}"


@pytest.mark.unit
def test_a_manager_the_cli_dropped_is_still_closed_at_exit():
    """cli/_common.get_connection builds a manager, returns only the connection and
    drops the manager. A weak registry collects it, and the atexit hook then finds
    nothing to close — silently, because the hook cannot tell "no managers" from
    "no managers left". Pinned here so a teardown that ever does more than shut a
    local socket still gets reached from the CLI path."""
    import gc

    import olvm_aiops.connection as conn_mod
    from olvm_aiops.config import AppConfig

    closed = {"n": 0}

    class _Conn:
        def close(self):
            closed["n"] += 1

    def like_get_connection():
        mgr = conn_mod.ConnectionManager(AppConfig(targets=()))
        conn = _Conn()
        mgr._connections["engine1"] = conn
        return conn  # the manager goes out of scope here, exactly as the CLI drops it

    conn = like_get_connection()
    gc.collect()
    conn_mod._close_all_managers()
    assert closed["n"] == 1, "a manager the CLI dropped was never closed at exit"
    assert conn is not None
