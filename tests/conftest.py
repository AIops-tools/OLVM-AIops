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
