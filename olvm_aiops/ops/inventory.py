"""Inventory reads: data centers, clusters and hosts.

Row shapes are built from a live OLVM 4.5.5 engine's JSON (see
``tests/fixtures/olvm-4.5.5-installing``): counts, sizes and flags arrive as
strings, links as ``{"href", "id"}``, and a value the engine does not report is
absent — so every field here is ``None`` when unreported, never a guessed 0.
"""

from __future__ import annotations

from typing import Any

from olvm_aiops.connection import _seg
from olvm_aiops.ops import _util as u


def _version(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    major, minor = u.as_int(value.get("major")), u.as_int(value.get("minor"))
    return f"{major}.{minor}" if major is not None and minor is not None else None


def datacenter_row(dc: dict) -> dict:
    return {
        "id": u.text(dc.get("id"), 64),
        "name": u.text(dc.get("name")),
        "status": u.text(dc.get("status"), 32),
        "compatibilityVersion": _version(dc.get("version")),
        "local": u.as_bool(dc.get("local")),
        "quotaMode": u.text(dc.get("quota_mode"), 32),
        "description": u.text(dc.get("description")),
    }


def cluster_row(cl: dict) -> dict:
    cpu = cl.get("cpu") if isinstance(cl.get("cpu"), dict) else {}
    policy = cl.get("memory_policy") if isinstance(cl.get("memory_policy"), dict) else {}
    over = policy.get("over_commit") if isinstance(policy.get("over_commit"), dict) else {}
    return {
        "id": u.text(cl.get("id"), 64),
        "name": u.text(cl.get("name")),
        "compatibilityVersion": _version(cl.get("version")),
        # An empty cpu.type is what a cluster with no host yet reports: say "unset".
        "cpuType": u.text(cpu.get("type")) or None,
        "dataCenterId": u.ref_id(cl.get("data_center")),
        "memoryOverCommitPct": u.as_int(over.get("percent")),
        "ballooningEnabled": u.as_bool(cl.get("ballooning_enabled")),
        "haReservation": u.as_bool(cl.get("ha_reservation")),
        "upgradeInProgress": u.as_bool(cl.get("upgrade_in_progress")),
    }


def host_row(h: dict) -> dict:
    spm = h.get("spm") if isinstance(h.get("spm"), dict) else {}
    summary = h.get("summary") if isinstance(h.get("summary"), dict) else {}
    cpu = h.get("cpu") if isinstance(h.get("cpu"), dict) else {}
    topo = cpu.get("topology") if isinstance(cpu.get("topology"), dict) else {}
    os_ = h.get("os") if isinstance(h.get("os"), dict) else {}
    os_version = os_.get("version") if isinstance(os_.get("version"), dict) else {}
    return {
        "id": u.text(h.get("id"), 64),
        "name": u.text(h.get("name")),
        "address": u.text(h.get("address")),
        "status": u.text(h.get("status"), 32),
        "statusDetail": u.text(h.get("status_detail")),
        "externalStatus": u.text(h.get("external_status"), 32),
        "clusterId": u.ref_id(h.get("cluster")),
        "spmStatus": u.text(spm.get("status"), 32),
        "spmPriority": u.as_int(spm.get("priority")),
        "memoryBytes": u.as_int(h.get("memory")),
        "maxSchedulingMemoryBytes": u.as_int(h.get("max_scheduling_memory")),
        "vmsActive": u.as_int(summary.get("active")),
        "vmsTotal": u.as_int(summary.get("total")),
        "cpuSockets": u.as_int(topo.get("sockets")),
        "cpuCoresPerSocket": u.as_int(topo.get("cores")),
        "cpuThreadsPerCore": u.as_int(topo.get("threads")),
        "osVersion": u.text(os_version.get("full_version")),
        "updateAvailable": u.as_bool(h.get("update_available")),
        "reinstallationRequired": u.as_bool(h.get("reinstallation_required")),
    }


def list_datacenters(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT) -> dict:
    """[READ] Data centers with status and compatibility version."""
    rows, truncated = u.fetch_page(conn, "/datacenters", "data_center", limit)
    return u.envelope("dataCenters", [datacenter_row(r) for r in rows], limit, truncated)


def list_clusters(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT) -> dict:
    """[READ] Clusters with compatibility version, CPU type and memory policy."""
    rows, truncated = u.fetch_page(conn, "/clusters", "cluster", limit)
    return u.envelope("clusters", [cluster_row(r) for r in rows], limit, truncated)


def list_hosts(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT, search: str | None = None) -> dict:
    """[READ] KVM hosts with status, SPM role, memory and VM counts.

    ``search`` is passed to the engine's query language verbatim (e.g.
    ``status=up``, ``cluster=Default``).
    """
    rows, truncated = u.fetch_page(conn, "/hosts", "host", limit, search=search)
    return u.envelope("hosts", [host_row(r) for r in rows], limit, truncated)


def get_host(conn: Any, host_id: str) -> dict:
    """[READ] One host by id."""
    if not str(host_id or "").strip():
        raise ValueError("host_id is required.")
    body = conn.get(f"/hosts/{_seg(host_id)}")
    if not isinstance(body, dict) or not body.get("id"):
        raise ValueError(f"The engine returned no host for id '{host_id}'.")
    return host_row(body)
