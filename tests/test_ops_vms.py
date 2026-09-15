"""VM reads against payloads from a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import vms

pytestmark = pytest.mark.unit

RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


def _conn(body) -> MagicMock:
    conn = MagicMock()
    conn.get.return_value = body
    return conn


def test_running_vm_row_types():
    row = vms.get_vm(_conn(run("vm_after")), "cb377fa6-a678-4311-88f3-27851e396f04")
    assert row["name"] == "lab-vm1" and row["status"] == "up"
    assert row["vcpus"] == 1 and row["memoryBytes"] == 268435456
    assert row["memoryGuaranteedBytes"] == 268435456
    assert row["highAvailability"] is False and row["haPriority"] == 0
    assert row["restartPendingForConfig"] is False and row["hostId"]
    assert row["startTime"].endswith("Z") and row["stopTime"].endswith("Z")


def test_a_vm_that_never_ran_has_no_start_time_not_epoch_zero():
    [row] = vms.list_vms(_conn(run("vms")))["vms"]
    assert row["status"] == "down" and row["startTime"] is None and row["hostId"] is None


def test_list_passes_search_and_measures_truncation():
    conn = _conn({"vm": [run("vm_after")] * 3})
    out = vms.list_vms(conn, limit=2, search="status=up")
    assert out["returned"] == 2 and out["truncated"] is True
    conn.get.assert_called_once_with("/vms", params={"max": "3", "search": "status=up"})


def test_statistics_keep_ints_and_floats_and_units():
    out = vms.vm_statistics(_conn(run("vm_statistics")), "vm-1")["statistics"]
    assert out["memory.installed"] == {"value": 268435456, "unit": "bytes"}
    assert isinstance(out["cpu.current.total"]["value"], (int, float))


def test_statistic_without_a_datum_is_none():
    body = {"statistic": [{"name": "memory.used", "unit": "bytes", "values": {"value": []}},
                          {"name": "x", "values": {"value": [{"datum": True}]}}]}
    out = vms.statistics(body)
    assert out["memory.used"]["value"] is None and out["x"]["value"] is None


def test_ids_are_encoded_and_empty_answers_refused():
    conn = _conn(run("vm_after"))
    vms.get_vm(conn, "../hosts")
    assert conn.get.call_args.args[0] == "/vms/..%2Fhosts"
    with pytest.raises(ValueError, match="no VM"):
        vms.get_vm(_conn({}), "gone")
