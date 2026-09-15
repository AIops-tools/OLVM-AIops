"""vm_health_rca against the live running VM plus one mutation per rule."""

from __future__ import annotations

import copy
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import diagnose as dg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """The fixtures' events are from 2026-09-15 02:xx UTC. Pin "now" so a 24 h event
    window does not turn these tests into a time bomb that fails a day later."""
    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)

RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"
LIVE_VM = json.loads((RUN / "vm_after.json").read_text())


def _conn(*vms: dict, events: dict | None = None) -> MagicMock:
    routes = {"/vms": {"vm": list(vms)}, "/events": events or {}}
    conn = MagicMock()
    conn.get.side_effect = lambda path, params=None: copy.deepcopy(routes[path])
    return conn


def _vm(**changes) -> dict:
    return {**copy.deepcopy(LIVE_VM), **changes}


def test_the_live_running_vm_is_healthy():
    out = dg.vm_health_rca(_conn(_vm()))
    assert out["findings"] == [] and out["healthy"] is True
    assert out["vmStatusCounts"] == {"up": 1}


@pytest.mark.parametrize("status", sorted(dg.VM_STUCK))
def test_stuck_states_are_high(status):
    [f] = dg.vm_health_rca(_conn(_vm(status=status)))["findings"]
    assert f["severity"] == "high" and f["cause"] == dg.VM_STUCK[status]


def test_paused_points_at_storage_space_first():
    [f] = dg.vm_health_rca(_conn(_vm(status="paused")))["findings"]
    assert "storage_capacity_rca" in f["action"]


def test_image_locked_is_medium_and_transitions_are_info():
    [locked] = dg.vm_health_rca(_conn(_vm(status="image_locked")))["findings"]
    [moving] = dg.vm_health_rca(_conn(_vm(status="migrating")))["findings"]
    assert locked["severity"] == "medium" and moving["severity"] == "info"


def test_down_is_normal_unless_the_vm_is_highly_available():
    assert dg.vm_health_rca(_conn(_vm(status="down")))["findings"] == []
    ha = _vm(status="down", high_availability={"enabled": "true", "priority": "1"})
    [f] = dg.vm_health_rca(_conn(ha))["findings"]
    assert f["severity"] == "high" and "high availability" in f["signal"]


def test_pending_config_restart_is_low():
    [f] = dg.vm_health_rca(_conn(_vm(next_run_configuration_exists="true")))["findings"]
    assert f["severity"] == "low"


def test_vm_events_attach_to_their_vm_and_rank_above_low_findings():
    vm = _vm(next_run_configuration_exists="true")
    events = {"event": [{"index": "9", "time": 1789439400722, "severity": "error", "code": "1100",
                         "description": "VM lab-vm1 is down with error.",
                         "vm": {"id": vm["id"]}}]}
    out = dg.vm_health_rca(_conn(vm, events=events))
    assert [(f["rank"], f["severity"]) for f in out["findings"]] == [(1, "high"), (2, "low")]
    assert "1100" in out["findings"][0]["signal"]


def test_an_error_event_from_before_the_vm_last_started_is_superseded_not_high():
    """Live lab: event 54 'Failed to run VM … disks are locked' at 02:28:15; the VM started
    successfully at 02:30:00 and is up. It must not stay flagged high."""
    vm = _vm()                                  # up, start_time 1789439400722
    events = {"event": [{"index": "154", "time": 1789439295422, "severity": "error", "code": "54",
                         "description": "Failed to run VM lab-vm1 due to a failed validation: "
                                        "[Cannot run VM: The following disks are locked]",
                         "vm": {"id": vm["id"]}}]}
    out = dg.vm_health_rca(_conn(vm, events=events))
    [f] = out["findings"]
    assert f["severity"] == "info" and "Superseded" in f["cause"]
    assert out["healthy"] is True


def test_an_error_event_after_the_current_start_still_counts():
    """Positive control: the same event AFTER the VM started is a live problem."""
    vm = _vm()
    events = {"event": [{"index": "160", "time": 1789439500000, "severity": "error", "code": "119",
                         "description": "VM lab-vm1 is down with error.", "vm": {"id": vm["id"]}}]}
    [f] = dg.vm_health_rca(_conn(vm, events=events))["findings"]
    assert f["severity"] == "high"


def test_a_failed_start_is_superseded_once_the_vm_started_even_if_it_is_down_again():
    """Review finding: a VM that failed to start, then started and was shut down normally,
    stayed high because only an 'up' VM could supersede the event."""
    vm = _vm(status="down", stop_time=1789440000000)          # started 1789439400722, stopped later
    events = {"event": [{"index": "154", "time": 1789439295422, "severity": "error", "code": "54",
                         "description": "Failed to run VM lab-vm1", "vm": {"id": vm["id"]}}]}
    [f] = dg.vm_health_rca(_conn(vm, events=events))["findings"]
    assert f["severity"] == "info" and "Superseded" in f["cause"]


def test_vm_events_outside_the_window_are_ignored():
    vm = _vm(status="down")
    vm.pop("start_time", None)
    events = {"event": [{"index": "1", "time": (1789441200 - 3 * 86400) * 1000, "severity": "error",
                         "code": "119", "description": "old", "vm": {"id": vm["id"]}}]}
    out = dg.vm_health_rca(_conn(vm, events=events))
    assert out["findings"] == [] and out["eventsOutsideWindow"] == 1
