"""Virtual machines: inventory, one VM, and its statistics.

Shapes verified on a live OLVM 4.5.5 engine (``tests/fixtures/olvm-4.5.5-running``):
counts and sizes are strings, ``start_time`` / ``stop_time`` are epoch-ms numbers
(``start_time`` absent on a VM that never ran), CPU topology is three strings, and
statistics are gauges whose ``datum`` is an int for bytes and a float for percentages.
"""

from __future__ import annotations

import re
from typing import Any

from olvm_aiops.connection import _seg
from olvm_aiops.ops import _util as u
from olvm_aiops.ops import inventory


def _cpu_count(cpu: Any) -> int | None:
    topo = cpu.get("topology") if isinstance(cpu, dict) and isinstance(cpu.get("topology"), dict) \
        else {}
    parts = [u.as_int(topo.get(k)) for k in ("sockets", "cores", "threads")]
    if any(p is None for p in parts):
        return None
    return parts[0] * parts[1] * parts[2]


def vm_row(vm: dict) -> dict:
    ha = vm.get("high_availability") if isinstance(vm.get("high_availability"), dict) else {}
    os_ = vm.get("os") if isinstance(vm.get("os"), dict) else {}
    policy = vm.get("memory_policy") if isinstance(vm.get("memory_policy"), dict) else {}
    return {
        "id": u.text(vm.get("id"), 64),
        "name": u.text(vm.get("name")),
        "status": u.text(vm.get("status"), 32),
        "statusDetail": u.text(vm.get("status_detail")),
        "hostId": u.ref_id(vm.get("host")),
        "clusterId": u.ref_id(vm.get("cluster")),
        "type": u.text(vm.get("type"), 32),
        "osType": u.text(os_.get("type"), 64),
        "vcpus": _cpu_count(vm.get("cpu")),
        "memoryBytes": u.as_int(vm.get("memory")),
        "memoryGuaranteedBytes": u.as_int(policy.get("guaranteed")),
        "highAvailability": u.as_bool(ha.get("enabled")),
        "haPriority": u.as_int(ha.get("priority")),
        "stateless": u.as_bool(vm.get("stateless")),
        "restartPendingForConfig": u.as_bool(vm.get("next_run_configuration_exists")),
        "startTime": u.ms_to_iso(vm.get("start_time")),
        "stopTime": u.ms_to_iso(vm.get("stop_time")),
    }


def list_vms(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT, search: str | None = None,
             page: int = 1) -> dict:
    """[READ] VMs with status, host, sizing and HA flag.

    ``search`` uses the engine query language (e.g. ``status=up``,
    ``cluster=Default``, ``host=kvm1``). The engine sizes each page with ``max``;
    it has no offset or total count. Page boundaries are stable only while the
    inventory stays unchanged between requests.
    """
    limit = u.bounded_limit(limit)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer.")
    if search is not None and not isinstance(search, str):
        raise ValueError("search must be a string.")
    criteria = (search or "").strip()
    if re.search(r"\bpage\s+\d+\b", criteria, re.IGNORECASE):
        raise ValueError("Pass page as the page argument, not inside search.")
    ordered = criteria if re.search(r"\bsortby\b", criteria, re.IGNORECASE) else (
        f"{criteria} sortby name asc" if criteria else "sortby name asc"
    )
    # Live OLVM 4.5.5 engines returned HTTP 200 with zero VMs for a bare
    # ``sortby name``. Give an explicit direction for caller-supplied bare sorts too.
    if re.search(r"\bsortby\s+\S+\s*$", ordered, re.IGNORECASE):
        ordered += " asc"

    def fetch(number: int) -> list[dict]:
        query = f"{ordered} page {number}"
        body = conn.get("/vms", params={"max": str(limit), "search": query})
        rows = u.items(body, "vm")
        if any(not isinstance(row, dict) for row in body.get("vm", [])):
            raise ValueError(f"Engine returned a non-object VM on page {number}.")
        if len(rows) > limit:
            raise ValueError(f"Engine returned more VMs than max={limit} on page {number}.")
        return rows

    rows = fetch(page)
    has_next = bool(fetch(page + 1)) if len(rows) == limit else False
    return {**u.envelope("vms", [vm_row(r) for r in rows], limit, has_next),
            "page": page, "nextPage": page + 1 if has_next else None, "total": None}


def capacity_summary(conn: Any, page_size: int = 100, max_pages: int = 100) -> dict:
    """[READ] Compact engine-wide VM/vCPU and host-core assignment summary.

    This walks stateless VM pages and returns aggregates, never a VM list. A
    completed walk is not an atomic snapshot: concurrent inventory changes can
    still move objects between pages without a detectable duplicate.
    """
    page_size = u.bounded_limit(page_size, "page_size")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be an integer between 1 and 100.")

    seen: set[str] = set()
    known_vcpus = 0
    missing_vcpus = 0
    pages_read = 0
    scan_complete = False
    reason: str | None = None
    for page in range(1, max_pages + 1):
        result = list_vms(conn, limit=page_size, page=page)
        pages_read += 1
        for vm in result["vms"]:
            vm_id = vm["id"]
            if not vm_id:
                reason = "missing_vm_id"
                break
            if vm_id in seen:
                reason = "duplicate_vm_id"
                break
            seen.add(vm_id)
            if vm["vcpus"] is None:
                missing_vcpus += 1
            else:
                known_vcpus += vm["vcpus"]
        if reason:
            break
        if result["nextPage"] is None:
            scan_complete = True
            break
    if not scan_complete and reason is None:
        reason = "page_limit"

    hosts_evaluated: int | None = None
    hosts_truncated: bool | None = None
    hosts_missing_cpu: int | None = None
    physical_cores: int | None = None
    if scan_complete:
        body = conn.get("/hosts", params={"max": "1001"})
        hosts = u.items(body, "host")
        if len(hosts) != len(body.get("host", [])):
            raise ValueError("Engine returned a non-object host during capacity scan.")
        hosts_truncated = len(hosts) > 1000
        host_rows = [inventory.host_row(row) for row in hosts[:1000]]
        hosts_evaluated = len(host_rows)
        hosts_missing_cpu = 0
        known_cores = 0
        host_ids: set[str] = set()
        for host in host_rows:
            host_id = host["id"]
            if not host_id:
                reason = "missing_host_id"
                break
            if host_id in host_ids:
                reason = "duplicate_host_id"
                break
            host_ids.add(host_id)
            sockets, cores = host["cpuSockets"], host["cpuCoresPerSocket"]
            if sockets is None or cores is None:
                hosts_missing_cpu += 1
            else:
                known_cores += sockets * cores
        if reason is None and not hosts_truncated and not hosts_missing_cpu:
            physical_cores = known_cores

    vm_count = len(seen) if scan_complete else None
    configured_vcpus = known_vcpus if scan_complete and not missing_vcpus else None
    ratio = (round(configured_vcpus / physical_cores * 100, 1)
             if configured_vcpus is not None and physical_cores is not None
             and physical_cores > 0 else None)
    if reason is None:
        if missing_vcpus:
            reason = "missing_vm_vcpus"
        elif hosts_truncated:
            reason = "hosts_truncated"
        elif hosts_missing_cpu:
            reason = "missing_host_cpu_topology"
        elif physical_cores == 0:
            reason = "no_physical_cores"
    return {
        "vmCount": vm_count,
        "observedVmCount": len(seen),
        "configuredVcpus": configured_vcpus,
        "observedConfiguredVcpus": known_vcpus,
        "vmsMissingVcpus": missing_vcpus,
        "hostsEvaluated": hosts_evaluated,
        "hostsTruncated": hosts_truncated,
        "hostsMissingCpuTopology": hosts_missing_cpu,
        "physicalCores": physical_cores,
        "assignmentRatioPct": ratio,
        "pagesRead": pages_read,
        "complete": reason is None,
        "incompleteReason": reason,
        "snapshotConsistent": False,
    }


def get_vm(conn: Any, vm_id: str) -> dict:
    """[READ] One VM by id."""
    if not str(vm_id or "").strip():
        raise ValueError("vm_id is required.")
    body = conn.get(f"/vms/{_seg(vm_id)}")
    if not isinstance(body, dict) or not body.get("id"):
        raise ValueError(f"The engine returned no VM for id '{vm_id}'.")
    return vm_row(body)


def statistics(body: Any, key: str = "statistic") -> dict[str, dict]:
    """Engine statistics as ``{name: {"value", "unit"}}``; a missing datum stays None."""
    out: dict[str, dict] = {}
    for stat in u.items(body, key):
        name = u.text(stat.get("name"), 64)
        if not name:
            continue
        values = stat.get("values") if isinstance(stat.get("values"), dict) else {}
        series = values.get("value") if isinstance(values.get("value"), list) else []
        datum = series[0].get("datum") if series and isinstance(series[0], dict) else None
        if isinstance(datum, bool) or not isinstance(datum, (int, float)):
            datum = None
        out[name] = {"value": datum, "unit": u.text(stat.get("unit"), 32)}
    return out


def vm_statistics(conn: Any, vm_id: str) -> dict:
    """[READ] A VM's current statistics (memory, CPU, network, disk usage)."""
    if not str(vm_id or "").strip():
        raise ValueError("vm_id is required.")
    return {"vmId": vm_id, "statistics": statistics(conn.get(f"/vms/{_seg(vm_id)}/statistics"))}
