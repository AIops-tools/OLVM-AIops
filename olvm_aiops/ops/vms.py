"""Virtual machines: inventory, one VM, and its statistics.

Shapes verified on a live OLVM 4.5.5 engine (``tests/fixtures/olvm-4.5.5-running``):
counts and sizes are strings, ``start_time`` / ``stop_time`` are epoch-ms numbers
(``start_time`` absent on a VM that never ran), CPU topology is three strings, and
statistics are gauges whose ``datum`` is an int for bytes and a float for percentages.
"""

from __future__ import annotations

from typing import Any

from olvm_aiops.connection import _seg
from olvm_aiops.ops import _util as u


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


def list_vms(conn: Any, limit: int = u.DEFAULT_LIST_LIMIT, search: str | None = None) -> dict:
    """[READ] VMs with status, host, sizing and HA flag.

    ``search`` uses the engine query language (e.g. ``status=up``,
    ``cluster=Default``, ``host=kvm1``).
    """
    rows, truncated = u.fetch_page(conn, "/vms", "vm", limit, search=search)
    return u.envelope("vms", [vm_row(r) for r in rows], limit, truncated)


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
