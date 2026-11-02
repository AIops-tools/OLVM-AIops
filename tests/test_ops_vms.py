"""VM reads against payloads from a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
import re
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
    conn = MagicMock()
    conn.get.side_effect = [{"vm": [run("vm_after")] * 2}, {"vm": [run("vm_after")]}]
    out = vms.list_vms(conn, limit=2, search="status=up")
    assert out["returned"] == 2 and out["truncated"] is True
    assert out["page"] == 1 and out["nextPage"] == 2 and out["total"] is None
    assert [call.kwargs["params"] for call in conn.get.call_args_list] == [
        {"max": "2", "search": "status=up sortby name page 1"},
        {"max": "2", "search": "status=up sortby name page 2"},
    ]


def test_vm_list_pages_cover_inventory_without_shifting_boundaries():
    """The fake implements the engine's max/page semantics, not a canned page."""
    inventory = [{"id": str(i), "name": f"vm-{i}", "cpu": {"topology": {
        "sockets": "1", "cores": str(i), "threads": "1"}}} for i in range(1, 6)]
    calls = []

    def get(path, params):
        assert path == "/vms"
        assert params["max"] == "2"  # max is page size, never limit + 1
        assert params["search"].startswith("sortby name page ")
        page = int(re.search(r"page (\d+)$", params["search"]).group(1))
        calls.append(page)
        return {"vm": inventory[(page - 1) * 2:page * 2]}

    conn = MagicMock()
    conn.get.side_effect = get
    first = vms.list_vms(conn, limit=2, page=1)
    second = vms.list_vms(conn, limit=2, page=first["nextPage"])
    third = vms.list_vms(conn, limit=2, page=second["nextPage"])
    assert [[vm["id"] for vm in page["vms"]] for page in (first, second, third)] == [
        ["1", "2"], ["3", "4"], ["5"]]
    assert [page["returned"] for page in (first, second, third)] == [2, 2, 1]
    assert [page["truncated"] for page in (first, second, third)] == [True, True, False]
    assert [page["nextPage"] for page in (first, second, third)] == [2, 3, None]
    assert all(page["total"] is None for page in (first, second, third))
    assert calls == [1, 2, 2, 3, 3]


def test_vm_list_preserves_explicit_sort_and_refuses_conflicting_page():
    conn = _conn({"vm": []})
    out = vms.list_vms(conn, search="status=up sortby name desc", page=3)
    assert out["page"] == 3 and out["nextPage"] is None
    conn.get.assert_called_once_with(
        "/vms", params={"max": "100", "search": "status=up sortby name desc page 3"})
    for bad in (0, -1, True, "2"):
        with pytest.raises(ValueError, match="page must"):
            vms.list_vms(conn, page=bad)
    with pytest.raises(ValueError, match="Pass page"):
        vms.list_vms(conn, search="status=up page 2")


def test_vm_list_refuses_malformed_rows_instead_of_claiming_last_page():
    conn = _conn({"vm": [{"id": "1", "name": "vm-1"}, None]})
    with pytest.raises(ValueError, match="non-object VM"):
        vms.list_vms(conn, limit=2)


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
