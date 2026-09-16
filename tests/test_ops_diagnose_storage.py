"""storage_capacity_rca: the live engine is healthy; each rule fires on its own signal."""

from __future__ import annotations

import copy
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from olvm_aiops.ops import diagnose as dg
from olvm_aiops.ops.storage import GIB

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    """The fixtures' events are from 2026-09-15 02:xx UTC. Pin "now" so a 24 h event
    window does not turn these tests into a time bomb that fails a day later."""
    monkeypatch.setattr(dg.time, "time", lambda: 1789441200)

RUN = pathlib.Path(__file__).parent / "fixtures" / "olvm-4.5.5-running"


def run(name: str) -> dict:
    return json.loads((RUN / f"{name}.json").read_text())


def _conn(mutate_global=None, mutate_dc=None) -> MagicMock:
    glob, dcv = run("storagedomains_global"), run("datacenter_storagedomains")
    nfs = next(s for s in glob["storage_domain"] if s["name"] == "lab-nfs-data")
    dc_id = nfs["data_centers"]["data_center"][0]["id"]
    if mutate_global:
        mutate_global(nfs)
    if mutate_dc:
        mutate_dc(dcv["storage_domain"][0])
    routes = {"/storagedomains": glob, f"/datacenters/{dc_id}/storagedomains": dcv}
    conn = MagicMock()
    routes.setdefault("/events", {})  # storage_capacity_rca also reads events
    conn.get.side_effect = lambda path, params=None: copy.deepcopy(routes[path])
    return conn


def _only(out: dict) -> dict:
    assert len(out["findings"]) == 1, out["findings"]
    return out["findings"][0]


def test_the_live_lab_storage_is_healthy_and_the_image_repository_is_skipped():
    out = dg.storage_capacity_rca(_conn())
    assert out["findings"] == [] and out["healthy"] is True
    assert out["domainsEvaluated"] == 1 and out["domainsSkippedUnattached"] == 1


def test_below_the_critical_blocker_is_critical_and_names_the_numbers():
    def low(sd):
        sd["available"], sd["used"] = str(3 * GIB), str(97 * GIB)
    f = _only(dg.storage_capacity_rca(_conn(mutate_global=low)))
    assert f["severity"] == "critical" and "3.0 GiB < critical blocker 5 GiB" in f["signal"]
    assert "refuses to create disks" in f["cause"]


def test_below_the_low_space_warning_but_above_the_blocker_is_medium():
    def lowish(sd):
        sd["available"], sd["used"] = str(40 * GIB), str(460 * GIB)   # 8% free, 40 GiB > 5
    f = _only(dg.storage_capacity_rca(_conn(mutate_global=lowish)))
    assert f["severity"] == "medium" and "free 8.00% < low-space warning 10%" in f["signal"]


def test_overcommit_alone_is_low_because_thin_provisioning_is_normal():
    def over(sd):
        sd["committed"] = str(120 * GIB)
    out = dg.storage_capacity_rca(_conn(mutate_global=over))
    f = _only(out)
    assert f["severity"] == "low" and "committed" in f["signal"]
    assert out["healthy"] is True


def test_overcommit_with_low_space_is_medium():
    def over_and_low(sd):
        sd["available"], sd["used"], sd["committed"] = str(40 * GIB), str(460 * GIB), str(600 * GIB)
    out = dg.storage_capacity_rca(_conn(mutate_global=over_and_low))
    assert sorted(f["severity"] for f in out["findings"]) == ["medium", "medium"]


@pytest.mark.parametrize(("status", "severity"), [
    ("inactive", "high"), ("unknown", "high"), ("mixed", "high"),
    ("maintenance", "info"), ("activating", "info")])
def test_attached_domain_state_from_the_data_center_view(status, severity):
    def set_status(row):
        row["status"] = status
    f = _only(dg.storage_capacity_rca(_conn(mutate_dc=set_status)))
    assert f["severity"] == severity and f"status={status}" == f["signal"]


def test_unreadable_status_is_medium_not_silently_fine():
    def drop(row):
        row.pop("status")
    f = _only(dg.storage_capacity_rca(_conn(mutate_dc=drop)))
    assert f["severity"] == "medium" and f["signal"] == "status not readable"


def test_findings_rank_worst_first():
    def bad(sd):
        sd["available"], sd["used"], sd["committed"] = str(2 * GIB), str(98 * GIB), str(150 * GIB)
    out = dg.storage_capacity_rca(_conn(mutate_global=bad))
    assert [(f["rank"], f["severity"]) for f in out["findings"]] == [(1, "critical"), (2, "medium")]
    assert "critical blocker" in out["findings"][0]["signal"]


def test_overcommit_alone_reads_as_a_planning_limit_and_shows_actual_use():
    """Production feedback: an FC domain 192 % committed but 43.7 % used. The severity was
    right; what the finding did not say is that nothing is under pressure yet."""
    def over(sd):
        sd["available"], sd["used"] = str(563 * GIB), str(437 * GIB)
        sd["committed"] = str(1920 * GIB)
    out = dg.storage_capacity_rca(_conn(mutate_global=over))
    f = _only(out)
    assert f["severity"] == "low" and out["healthy"] is True
    assert f["signal"] == "committed 192.0% of capacity, in use 43.7% (56.3% free)"
    assert "planning limit, not current pressure" in f["cause"]
    assert "above the thresholds the engine set" in f["cause"]
    assert "before it reaches its low-space threshold (10% free)" in f["action"]


def test_overcommit_with_low_space_says_the_promise_cannot_be_kept():
    def over_and_low(sd):
        sd["available"], sd["used"], sd["committed"] = str(40 * GIB), str(460 * GIB), str(600 * GIB)
    out = dg.storage_capacity_rca(_conn(mutate_global=over_and_low))
    f = next(f for f in out["findings"] if f["signal"].startswith("committed"))
    assert f["severity"] == "medium" and "already low on space" in f["cause"]
    assert "planning limit" not in f["cause"]


@pytest.mark.parametrize("value", [None, "0"])
def test_without_a_threshold_the_finding_does_not_claim_the_space_is_comfortable(value):
    """Review: at 5 % free and no threshold, the cause said "still within the domain's own
    thresholds" and the action said "no action" — a check that never happened. The engine
    really does report 0 (live: the image repository), and 0 can never be crossed."""
    def no_thresholds(sd):
        sd["available"], sd["used"] = str(5 * GIB), str(95 * GIB)
        sd["committed"] = str(150 * GIB)
        sd["warning_low_space_indicator"] = value
        sd["critical_space_action_blocker"] = value
    f = _only(dg.storage_capacity_rca(_conn(mutate_global=no_thresholds)))
    assert "5.0% free" in f["signal"]
    assert "no check was made" in f["cause"] and "planning limit" not in f["cause"]
    assert "No action" not in f["action"]
    text = f["signal"] + f["cause"] + f["action"]
    assert "None" not in text  # a missing threshold is never quoted back as "None GiB"


def test_the_critical_blocker_is_named_when_the_engine_reports_it():
    def lowish(sd):
        sd["available"], sd["used"] = str(40 * GIB), str(460 * GIB)
    f = _only(dg.storage_capacity_rca(_conn(mutate_global=lowish)))
    assert "the critical blocker (5 GiB)" in f["action"]


def test_a_low_space_warning_without_a_blocker_does_not_quote_a_none_threshold():
    def no_blocker(sd):
        sd["available"], sd["used"] = str(40 * GIB), str(460 * GIB)   # 8 % free < 10 %
        sd["critical_space_action_blocker"] = None
    f = _only(dg.storage_capacity_rca(_conn(mutate_global=no_blocker)))
    assert f["severity"] == "medium" and "None" not in f["action"]
    assert f["action"].endswith("before it reaches the critical blocker.")
