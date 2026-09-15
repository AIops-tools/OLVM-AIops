"""MCP read-tool bodies, the _shared error envelope, server entrypoints, and
the undo CLI — all against mocked connections/stores (no live engine, no stdio run).
"""

from __future__ import annotations

import sqlite3

import pytest

import olvm_aiops.governance.audit as audit_mod
import olvm_aiops.governance.policy as policy_mod
import olvm_aiops.governance.undo as undo_mod


@pytest.fixture
def gov_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv("OLVM_AUDIT_APPROVED_BY", "pytest")
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()


# ─── _shared error helpers ──────────────────────────────────────────────────


@pytest.mark.unit
def test_tool_errors_shapes():
    from mcp_server._shared import tool_errors

    @tool_errors("dict")
    def d():
        raise KeyError("k")

    @tool_errors("list")
    def li():
        raise KeyError("k")

    @tool_errors("str")
    def st():
        raise KeyError("k")

    assert d()["error"]
    assert li()[0]["error"]
    assert st().startswith("Error:")


@pytest.mark.unit
def test_safe_error_masks_unexpected_exception_type():
    from mcp_server._shared import _safe_error

    # A passthrough type keeps its message...
    assert "boom" in _safe_error(ValueError("boom"), "t")

    # ...an unexpected type is masked to a generic string (no leak).
    class WeirdError(Exception):
        pass

    masked = _safe_error(WeirdError("secret-internal-detail"), "t")
    assert "secret-internal-detail" not in masked
    assert masked == "WeirdError: operation failed."


@pytest.mark.unit
def test_get_connection_lazy_inits_manager(monkeypatch, tmp_path):
    import mcp_server._shared as shared

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("targets:\n  - name: engine1\n    url: https://engine.example\n"
                        "    username: admin@internal\n")
    monkeypatch.setenv("OLVM_AIOPS_CONFIG", str(cfg_file))
    monkeypatch.setattr(shared, "_conn_mgr", None)
    built: list[str] = []

    class FakeConn:  # the real connection logs in on construction
        def __init__(self, target):
            built.append(target.name)
            self.target = target

        def close(self):
            pass

    monkeypatch.setattr("olvm_aiops.connection.OlvmConnection", FakeConn)
    conn = shared._get_connection()
    assert conn.target.name == "engine1"
    # second call reuses the lazily built manager and its cached connection
    assert shared._get_connection() is conn and built == ["engine1"]


# ─── server / root entrypoints ──────────────────────────────────────────────


@pytest.mark.unit
def test_server_main_runs_transport(monkeypatch):
    import mcp_server.server as server

    called = {}
    monkeypatch.setattr(server.mcp, "run", lambda transport: called.setdefault("t", transport))
    server.main()
    assert called["t"] == "stdio"


@pytest.mark.unit
def test_root_mcp_command_invokes_server_main(monkeypatch):
    from typer.testing import CliRunner

    import mcp_server.server as server
    from olvm_aiops.cli import app

    ran = {}
    monkeypatch.setattr(server, "main", lambda: ran.setdefault("ok", True))
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 0, result.output
    assert ran["ok"] is True


@pytest.mark.unit
def test_root_mcp_command_rejects_old_python(monkeypatch):
    import collections
    import sys

    from typer.testing import CliRunner

    from olvm_aiops.cli import app

    vi = collections.namedtuple("vi", "major minor micro releaselevel serial")
    monkeypatch.setattr(sys, "version_info", vi(3, 10, 0, "final", 0))
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 2
    assert "requires Python" in result.output


# ─── undo CLI ───────────────────────────────────────────────────────────────


def _record(undo_tool="_undo_probe2", params=None):
    descriptor = {"tool": undo_tool, "params": params or {"value": "v"}}
    return undo_mod.get_undo_store().record(
        skill="probe", tool="orig_op", undo_descriptor=descriptor
    )


@pytest.fixture
def undo_probe(gov_home):
    from mcp_server._shared import mcp
    from olvm_aiops.governance import governed_tool

    calls: list[dict] = []

    @governed_tool(risk_level="low")
    def _undo_probe2(value: str = "", target=None) -> dict:
        calls.append({"value": value})
        return {"ok": True}

    mcp.add_tool(_undo_probe2, name="_undo_probe2")
    yield calls
    mcp._tool_manager._tools.pop("_undo_probe2", None)


@pytest.mark.unit
def test_cli_undo_list_renders(undo_probe):
    from typer.testing import CliRunner

    from olvm_aiops.cli import app

    uid = _record()
    result = CliRunner().invoke(app, ["undo", "list"])
    assert result.exit_code == 0, result.output
    assert uid in result.output


@pytest.mark.unit
def test_cli_undo_apply_confirmed_dispatches_inverse(gov_home, undo_probe):
    from typer.testing import CliRunner

    from olvm_aiops.cli import app

    uid = _record(params={"value": "restore"})
    result = CliRunner().invoke(app, ["undo", "apply", uid], input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert undo_probe == [{"value": "restore"}]
    assert undo_mod.get_undo_store().get(uid)["status"] == "applied"
    # audited on the governance path
    tools = _audit_tools(gov_home / "audit.db")
    assert "undo_apply" in tools and "_undo_probe2" in tools


@pytest.mark.unit
def test_undo_apply_passes_target_and_bad_params(gov_home, undo_probe):
    from mcp_server.tools import undo as gov

    # Corrupt undo_params must REFUSE, not fall back to {} and dispatch.
    #
    # This test previously asserted the opposite ("falls back to {} and still
    # dispatches", applied is True) — it had encoded the defect as the spec.
    # Defaulting called the inverse tool with NO ARGUMENTS: where every
    # parameter has a default that runs a real, unintended operation, and the
    # dry-run rendered it as a legitimate no-arg inverse. The recorded intent is
    # gone, so there is nothing that can honestly be applied.
    store = undo_mod.get_undo_store()
    uid = store.record(
        skill="probe", tool="orig", undo_descriptor={"tool": "_undo_probe2", "params": {}}
    )
    # corrupt the persisted params directly
    import olvm_aiops.governance.paths as paths

    db = paths.ops_home() / "undo.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE undo_log SET undo_params=? WHERE undo_id=?", ("{bad", uid))
        conn.commit()
    finally:
        conn.close()
    out = gov.undo_apply(undo_id=uid, target="engine1")
    assert "applied" not in out, "a refusal must not report an outcome"
    assert "unreadable recorded parameters" in out["error"]
    assert "_undo_probe2" in out["error"], "the error names the inverse to run by hand"


def _audit_tools(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log ORDER BY id")]
    finally:
        conn.close()
