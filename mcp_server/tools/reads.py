"""Read-only MCP tools: inventory, activity and host diagnosis."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from olvm_aiops.governance import governed_tool
from olvm_aiops.ops import activity, diagnose, inventory


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def datacenter_list(limit: int = 100, target: Optional[str] = None) -> dict:
    """[READ] Data centers with status (up, uninitialized, maintenance…) and compat version.

    Args:
        limit: Rows to return, 1-1000 (default 100); `truncated` says when more exist.
        target: Engine target name from config; omit to use the default.
    """
    return inventory.list_datacenters(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def cluster_list(limit: int = 100, target: Optional[str] = None) -> dict:
    """[READ] Clusters with compatibility version, CPU type and memory over-commit policy.

    Args:
        limit: Rows to return, 1-1000 (default 100).
        target: Engine target name from config; omit to use the default.
    """
    return inventory.list_clusters(_get_connection(target), limit=limit)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def host_list(limit: int = 100, search: Optional[str] = None,
              target: Optional[str] = None) -> dict:
    """[READ] KVM hosts with status, SPM role, memory and VM counts.

    A field the engine did not report is null, never 0. For "what is wrong with my
    hosts" use host_health_rca instead of reading this list yourself.

    Args:
        limit: Rows to return, 1-1000 (default 100); `truncated` says when more exist.
        search: Engine query language, e.g. "status=up" or "status!=up".
        target: Engine target name from config; omit to use the default.
    """
    return inventory.list_hosts(_get_connection(target), limit=limit, search=search)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def host_get(host_id: str, target: Optional[str] = None) -> dict:
    """[READ] One KVM host by id (see host_list).

    Args:
        host_id: Host id.
        target: Engine target name from config; omit to use the default.
    """
    return inventory.get_host(_get_connection(target), host_id)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def event_list(limit: int = 100, min_severity: str = "normal", page: int = 1,
               after_index: Optional[int] = None, since_minutes: Optional[int] = None,
               target: Optional[str] = None) -> dict:
    """[READ] Engine events, newest first, at or above a severity.

    To follow new events, pass the highest `index` you have already seen as
    `after_index`. `since_minutes` keeps events from the last N minutes and sets
    `scanTruncated` when older ones in the window may be missing. SSO session ids
    in login events are redacted.

    Args:
        limit: Rows to return, 1-1000 (default 100); `truncated` says when more exist.
        min_severity: One of normal, warning, error, alert (default normal).
        page: Older pages of events (not combinable with since_minutes).
        after_index: Only events with a higher index than this.
        since_minutes: Only events from the last N minutes (1-129600).
        target: Engine target name from config; omit to use the default.
    """
    return activity.list_events(_get_connection(target), limit=limit, min_severity=min_severity,
                                page=page, after_index=after_index, since_minutes=since_minutes)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def job_list(limit: int = 100, status: Optional[str] = None,
             target: Optional[str] = None) -> dict:
    """[READ] Engine jobs (long-running operations) with status and duration.

    Args:
        limit: Rows to return, 1-1000 (default 100).
        status: Only one status: started, finished, failed, aborted, unknown.
        target: Engine target name from config; omit to use the default.
    """
    return activity.list_jobs(_get_connection(target), limit=limit, status=status)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def host_health_rca(events_limit: int = 200, events_window_hours: int = 24,
                    target: Optional[str] = None) -> dict:
    """[READ] What needs attention on KVM hosts, ranked worst first, in one call.

    Combines host status and status detail, reinstall/update flags and recent
    warning-or-worse events that name a host. Each finding has `signal` (what was
    measured), `cause`, `action` and `rank`. Hosts that the engine is installing or
    rebooting are reported as in progress, not failed; alert 9000 (power management
    not verifiable) is informational on hosts without fencing hardware. Report
    findings in rank order and quote their signal.

    Args:
        events_limit: Recent warning-or-worse events to correlate, 1-1000 (default 200).
        events_window_hours: Ignore events older than this many hours, 1-720 (default 24);
            older ones are counted in eventsOutsideWindow.
        target: Engine target name from config; omit to use the default.
    """
    return diagnose.host_health_rca(_get_connection(target), events_limit=events_limit,
                                    events_window_hours=events_window_hours)
