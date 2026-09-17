"""CLI: data centers, clusters and hosts (reads) plus host diagnosis.

Every command calls the MCP tool of the same name in ``mcp_server.tools``, so a CLI
read runs through the same ``@governed_tool`` harness as the MCP server: an audit
row, the budget and the runaway breaker. Calling ``olvm_aiops.ops`` directly would
leave no trace in the audit log.
"""

from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from olvm_aiops.cli._common import TargetOption, cli_errors, console, governed

datacenter_app = typer.Typer(help="Data centers.", no_args_is_help=True)
cluster_app = typer.Typer(help="Clusters.", no_args_is_help=True)
host_app = typer.Typer(help="KVM hosts: list, get, health diagnosis.", no_args_is_help=True)
engine_app = typer.Typer(help="The engine itself: health diagnosis.", no_args_is_help=True)

LimitOption = typer.Option(100, "--limit", help="Rows to show (1-1000).")
JsonOption = typer.Option(False, "--json", help="Print the full payload as JSON.")
EventsLimitOption = typer.Option(200, "--events-limit", min=1, max=1000,
                                 help="Recent warning+ events to correlate (1-1000).")
EventsWindowOption = typer.Option(24, "--events-window-hours", min=1, max=720,
                                  help="Ignore events older than this many hours.")


def _cell(value: object) -> str:
    return "-" if value is None else escape(str(value))


def _table(title: str, cols: list[str], rows: list[dict]) -> None:
    table = Table(title=title)
    for col in cols:
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(*[_cell(row.get(c)) for c in cols])
    console.print(table)


def _footer(out: dict) -> None:
    if out.get("truncated"):
        console.print(f"[yellow]Showing {out['returned']} of more — raise --limit.[/]")


def print_json(out: dict) -> None:
    console.print_json(json.dumps(out))


def print_findings(out: dict, subject_key: str) -> None:
    if not out["findings"]:
        console.print("[green]No findings.[/]")
    for f in out["findings"]:
        colour = {"critical": "red", "high": "red", "medium": "yellow"}.get(f["severity"], "dim")
        # The severity label is bracketed; unescaped, rich reads "[info]" as markup and drops it.
        label = escape(f"[{f['severity']}]")
        console.print(f"[{colour}]{f['rank']}. {label} {escape(f.get(subject_key) or '-')}: "
                      f"{escape(f['signal'])}[/]")
        console.print(f"   cause: {escape(f['cause'])}\n   action: {escape(f['action'])}")
        _print_candidates(f.get("vmCandidates"))
        _print_related_events(f.get("relatedVmEvents"))


def _print_related_events(related: dict | None) -> None:
    """The VM-level failures the engine logged beside a vdsm wrapper failure.

    The wrapper carries no VM reference, so these are paired on time and host: printed as
    candidates, and printed even when there are none — "the engine logged nothing beside
    this" is a measurement, and silence would read as "this was not looked at".
    """
    if related is None:
        return
    rows = ", ".join(
        f"{escape(r['vm'] or r['vmId'] or '-')} (event {r['code']}, {r['secondsApart']:+d}s)"
        for r in related["events"])
    none = f"none logged within {related['windowSeconds']}s"
    console.print(f"   Related VM events (not confirmed): {rows or none}")
    for r in related["events"]:
        console.print(f"     {escape(r['description'])}")
    if related["truncated"]:
        console.print(f"   [yellow]PARTIAL: only the {related['limit']} nearest are shown; "
                      "read event_list for the rest.[/]")


def _print_candidates(candidates: dict | None) -> None:
    """The VMs a finding could be about — never printed as the VM it is about."""
    if candidates is None:
        return
    if candidates["error"] is not None:
        console.print("   [yellow]VM candidates: not readable — "
                      f"{escape(candidates['error'])}[/]")
        return
    names = ", ".join(escape(v["name"] or v["id"] or "-") for v in candidates["vms"])
    more = (f" (+{candidates['total'] - candidates['returned']} more)"
            if candidates["truncated"] else "")
    console.print(f"   VM candidates (not confirmed): {names or 'none on this host'}{more}")
    if candidates["scanTruncated"]:
        console.print("   [yellow]PARTIAL: the VM scan was cut short, so this list and its "
                      "count are a lower bound.[/]")


@datacenter_app.command("list")
@cli_errors
def datacenter_list(limit: int = LimitOption, as_json: bool = JsonOption,
                    target: TargetOption = None) -> None:
    """Data centers with status and compatibility version."""
    from mcp_server.tools import reads

    out = governed(reads.datacenter_list(limit=limit, target=target))
    if as_json:
        print_json(out)
        return
    _table("Data centers", ["name", "status", "compatibilityVersion", "local", "id"],
           out["dataCenters"])
    _footer(out)


@cluster_app.command("list")
@cli_errors
def cluster_list(limit: int = LimitOption, as_json: bool = JsonOption,
                 target: TargetOption = None) -> None:
    """Clusters with compatibility version and CPU type."""
    from mcp_server.tools import reads

    out = governed(reads.cluster_list(limit=limit, target=target))
    if as_json:
        print_json(out)
        return
    _table("Clusters", ["name", "compatibilityVersion", "cpuType", "memoryOverCommitPct", "id"],
           out["clusters"])
    _footer(out)


@host_app.command("list")
@cli_errors
def host_list(limit: int = LimitOption,
              search: str | None = typer.Option(None, "--search",
                                                help="Engine query, e.g. 'status!=up'."),
              as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """KVM hosts with status, SPM role and VM counts."""
    from mcp_server.tools import reads

    out = governed(reads.host_list(limit=limit, search=search, target=target))
    if as_json:
        print_json(out)
        return
    _table("Hosts", ["name", "status", "statusDetail", "spmStatus", "vmsActive", "address"],
           out["hosts"])
    _footer(out)


@host_app.command("get")
@cli_errors
def host_get(host_id: str = typer.Argument(..., help="Host id (see 'host list')."),
             target: TargetOption = None) -> None:
    """One host, as JSON."""
    from mcp_server.tools import reads

    print_json(governed(reads.host_get(host_id=host_id, target=target)))


@host_app.command("health")
@cli_errors
def host_health(events_window_hours: int = EventsWindowOption,
                events_limit: int = EventsLimitOption,
                as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """What needs attention on hosts, worst first."""
    from mcp_server.tools import reads

    out = governed(reads.host_health_rca(events_limit=events_limit,
                                         events_window_hours=events_window_hours,
                                         target=target))
    if as_json:
        print_json(out)
        return
    counts = ", ".join(f"{k}={v}" for k, v in sorted(out["hostStatusCounts"].items())) or "none"
    console.print(f"{out['hostsEvaluated']} host(s): {escape(counts)}")
    print_findings(out, "host")
    if out["hostsTruncated"] or out["eventsTruncated"]:
        console.print("[yellow]PARTIAL: the host or event scan was cut short.[/]")


@engine_app.command("health")
@cli_errors
def engine_health(events_window_hours: int = EventsWindowOption,
                  events_limit: int = EventsLimitOption,
                  as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """Problems with the engine itself (health check, clock, certificates, backups)."""
    from mcp_server.tools import reads

    out = governed(reads.engine_health_rca(events_limit=events_limit,
                                           events_window_hours=events_window_hours,
                                           target=target))
    if as_json:
        print_json(out)
        return
    status = out["healthServlet"]["status"]
    skew = out["clockSkewSeconds"]
    version = out["engineVersion"] or "version not reported"
    console.print(f"Engine {escape(version)}; health check "
                  f"{'unreadable' if status is None else f'HTTP {status}'}; clock skew "
                  f"{'not reported' if skew is None else f'{skew:+.1f} s'}")
    print_findings(out, "subject")
    if out["eventsTruncated"]:
        console.print("[yellow]PARTIAL: the event scan was cut short.[/]")
