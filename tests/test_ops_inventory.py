"""Inventory rows built from payloads captured on a live OLVM 4.5.5 engine."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import inventory as inv

pytestmark = pytest.mark.unit

LIVE = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-installing"


def live(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def _conn(body: dict) -> MagicMock:
    conn = MagicMock()
    conn.get.return_value = body
    return conn


def test_datacenter_row_from_live_payload():
    out = inv.list_datacenters(_conn(live("datacenters")))
    [dc] = out["dataCenters"]
    assert dc["name"] == "Default" and dc["status"] == "uninitialized"
    assert dc["compatibilityVersion"] == "4.7" and dc["local"] is False
    assert out["returned"] == 1 and out["truncated"] is False


def test_cluster_row_types_are_real_types_not_strings():
    [cl] = inv.list_clusters(_conn(live("clusters")))["clusters"]
    assert cl["compatibilityVersion"] == "4.7"
    assert cl["memoryOverCommitPct"] == 100 and type(cl["memoryOverCommitPct"]) is int
    assert cl["ballooningEnabled"] is True and cl["haReservation"] is False
    assert cl["cpuType"] is None, "an empty cpu.type is unset, not a CPU named ''"


def test_installing_host_reports_zeros_as_ints_and_absent_values_as_none():
    [h] = inv.list_hosts(_conn(live("hosts")))["hosts"]
    assert h["name"] == "olvm-kvm1" and h["status"] == "installing"
    assert h["statusDetail"] is None
    assert h["memoryBytes"] == 0 and type(h["memoryBytes"]) is int
    assert h["vmsTotal"] == 0 and h["vmsActive"] is None, "summary had no 'active' key"
    assert h["spmStatus"] == "none" and h["spmPriority"] == 5
    assert h["updateAvailable"] is False and h["reinstallationRequired"] is False
    assert h["cpuSockets"] is None, "an empty topology reports nothing"
    assert h["clusterId"]


def test_host_listing_asks_for_one_extra_row_and_passes_search():
    conn = _conn({"host": [live("hosts")["host"][0]] * 3})
    out = inv.list_hosts(conn, limit=2, search="status=up")
    assert out["returned"] == 2 and out["truncated"] is True
    conn.get.assert_called_once_with("/hosts", params={"max": "3", "search": "status=up"})


def test_empty_collection_body_is_an_empty_listing():
    assert inv.list_hosts(_conn({}))["hosts"] == []


def test_get_host_encodes_the_id_and_refuses_an_empty_answer():
    conn = _conn(live("hosts")["host"][0])
    assert inv.get_host(conn, "../clusters")["name"] == "olvm-kvm1"
    assert conn.get.call_args.args[0] == "/hosts/..%2Fclusters"
    with pytest.raises(ValueError, match="no host"):
        inv.get_host(_conn({}), "missing")
    with pytest.raises(ValueError, match="required"):
        inv.get_host(_conn({}), " ")
