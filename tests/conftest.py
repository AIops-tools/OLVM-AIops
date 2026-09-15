"""Shared fixtures for the olvm-aiops test suite (no live OLVM engine).

The REST connection is always a MagicMock/fake; these fixtures only shape the
governance environment the tools run under.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_approver(monkeypatch):
    """The approver is an optional audit annotation now, not a gate: record a
    synthetic one globally so audit rows carry a who; the governance-persistence
    tests remove it to prove a high-risk write runs without one."""
    monkeypatch.setenv("OLVM_AUDIT_APPROVED_BY", "pytest")


@pytest.fixture(autouse=True)
def _isolated_governance_home(tmp_path_factory, monkeypatch):
    """Audit, undo and budget state live in a throwaway home for every test.

    CLI reads run through the governed MCP tools, so they write audit rows; without
    this a test run would write into the developer's real ~/.olvm-aiops. Tests that
    need a specific home still set their own."""
    import olvm_aiops.governance.audit as audit_mod
    import olvm_aiops.governance.budget as budget_mod
    import olvm_aiops.governance.undo as undo_mod

    resets = (audit_mod.reset_engine, undo_mod.reset_undo_store, budget_mod.reset_budget)
    monkeypatch.setenv("OLVM_AIOPS_HOME", str(tmp_path_factory.mktemp("olvm-home")))
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()
