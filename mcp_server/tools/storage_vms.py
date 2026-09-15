"""Read-only MCP tools: storage domains, VMs and storage capacity diagnosis."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from olvm_aiops.governance import governed_tool
from olvm_aiops.ops import diagnose, storage, vms


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def storage_domain_list(limit: int = 100, target: Optional[str] = None) -> dict:
    """[READ] Storage domains with status and capacity (free, used, committed bytes and %).

    The status of an attached domain comes from its data center (`statusSource`);
    it is null — not guessed — when that could not be read (`statusErrors`).

    Args:
        limit: Rows to return, 1-1000 (default 100); `truncated` says when more exist.
        target: Engine target name from config; omit to use the default.
    """
    return storage.list_storage_domains(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def storage_domain_get(domain_id: str, target: Optional[str] = None) -> dict:
    """[READ] One storage domain by id, with its data-center-scoped status.

    Args:
        domain_id: Storage domain id (see storage_domain_list).
        target: Engine target name from config; omit to use the default.
    """
    return storage.get_storage_domain(_get_connection(target), domain_id)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def vm_list(limit: int = 100, search: Optional[str] = None,
            target: Optional[str] = None) -> dict:
    """[READ] VMs with status, host, vCPUs, memory and high-availability flag.

    Args:
        limit: Rows to return, 1-1000 (default 100); `truncated` says when more exist.
        search: Engine query language, e.g. "status=up" or "cluster=Default".
        target: Engine target name from config; omit to use the default.
    """
    return vms.list_vms(_get_connection(target), limit=limit, search=search)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def vm_get(vm_id: str, target: Optional[str] = None) -> dict:
    """[READ] One VM by id (see vm_list).

    Args:
        vm_id: VM id.
        target: Engine target name from config; omit to use the default.
    """
    return vms.get_vm(_get_connection(target), vm_id)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def vm_stats(vm_id: str, target: Optional[str] = None) -> dict:
    """[READ] A VM's current statistics: memory, CPU %, network and disk usage, with units.

    Args:
        vm_id: VM id.
        target: Engine target name from config; omit to use the default.
    """
    return vms.vm_statistics(_get_connection(target), vm_id)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def storage_capacity_rca(target: Optional[str] = None) -> dict:
    """[READ] Storage-domain problems ranked worst first, in one call.

    Critical when free space is below the engine's critical blocker (the engine then
    refuses new disks and snapshots); medium below the low-space warning or when thin
    disks are committed beyond capacity; high when an attached domain is inactive,
    unknown or mixed. Unattached domains such as the default image repository are
    skipped. Report findings in rank order and quote their signal.

    Args:
        target: Engine target name from config; omit to use the default.
    """
    return diagnose.storage_capacity_rca(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def vm_health_rca(events_limit: int = 200, events_window_hours: int = 24,
                  target: Optional[str] = None) -> dict:
    """[READ] VM problems ranked worst first, in one call.

    High for VMs stuck not_responding/unknown, paused (often storage I/O errors or a
    full domain — check storage_capacity_rca), or down with high availability; medium
    for image_locked; info for VMs mid-transition; low for config changes waiting for
    a restart. Recent warning-or-worse events naming a VM are attached to it. Down VMs
    without HA are normal. Report findings in rank order and quote their signal.

    Args:
        events_limit: Recent warning-or-worse events to correlate, 1-1000 (default 200).
        events_window_hours: Ignore events older than this many hours, 1-720 (default 24);
            older ones are counted in eventsOutsideWindow.
        target: Engine target name from config; omit to use the default.
    """
    return diagnose.vm_health_rca(_get_connection(target), events_limit=events_limit,
                                  events_window_hours=events_window_hours)
