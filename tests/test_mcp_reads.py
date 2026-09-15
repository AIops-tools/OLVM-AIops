"""Read MCP tools delegate to the ops layer and fail into the error envelope."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

import olvm_aiops.governance.audit as audit_mod
import olvm_aiops.governance.undo as undo_mod
from mcp_server.tools import reads

pytestmark = pytest.mark.unit

LIVE = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-installing"


@pytest.fixture(autouse=True)
def gov_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path))
    audit_mod.reset_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    undo_mod.reset_undo_store()


def _wire(monkeypatch, routes: dict) -> MagicMock:
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: routes[path]
    monkeypatch.setattr(reads, "_get_connection", lambda target=None: conn)
    return conn


def live(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def test_read_tools_reach_their_ops(monkeypatch):
    _wire(monkeypatch, {"/datacenters": live("datacenters"), "/clusters": live("clusters"),
                        "/hosts": live("hosts"), "/events": live("events"), "/jobs": live("jobs")})
    assert reads.datacenter_list()["dataCenters"][0]["name"] == "Default"
    assert reads.cluster_list()["clusters"][0]["compatibilityVersion"] == "4.7"
    assert reads.host_list(search="status!=up")["hosts"][0]["status"] == "installing"
    assert reads.event_list(limit=5)["returned"] == 5
    assert reads.job_list(status="started")["jobs"][0]["status"] == "started"
    assert reads.host_health_rca()["hostsEvaluated"] == 1


def test_host_get_reaches_the_single_object_path(monkeypatch):
    host = live("hosts")["host"][0]
    conn = MagicMock()
    conn.get.return_value = host
    monkeypatch.setattr(reads, "_get_connection", lambda target=None: conn)
    assert reads.host_get(host_id=host["id"])["name"] == "olvm-kvm1"


def test_bad_arguments_come_back_as_the_error_envelope(monkeypatch):
    _wire(monkeypatch, {})
    out = reads.event_list(min_severity="critical")
    assert "min_severity" in out["error"] and "doctor" in out["hint"]


def test_read_tools_are_audited(monkeypatch, gov_home):
    import sqlite3

    _wire(monkeypatch, {"/hosts": live("hosts")})
    reads.host_list()
    conn = sqlite3.connect(gov_home / "audit.db")
    try:
        rows = conn.execute("SELECT tool, risk_level FROM audit_log").fetchall()
    finally:
        conn.close()
    assert ("host_list", "low") in rows


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """Fixture events are from 2026-09-15 02:xx UTC. Pin "now" so the 24 h event window
    does not turn these tests into a time bomb that fails a day later."""
    import olvm_aiops.ops.diagnose as dg

    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)  # 2026-09-15T03:00:00Z
