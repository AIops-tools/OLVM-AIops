"""CLI: data centers, clusters and hosts (reads) plus host diagnosis."""

from __future__ import annotations

import json

import typer
from rich.markup import escape
from rich.table import Table

from olvm_aiops.cli._common import TargetOption, cli_errors, console, get_connection
from olvm_aiops.ops import diagnose, inventory

datacenter_app = typer.Typer(help="Data centers.", no_args_is_help=True)
cluster_app = typer.Typer(help="Clusters.", no_args_is_help=True)
host_app = typer.Typer(help="KVM hosts: list, get, health diagnosis.", no_args_is_help=True)

LimitOption = typer.Option(100, "--limit", help="Rows to show (1-1000).")
JsonOption = typer.Option(False, "--json", help="Print the full payload as JSON.")


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


@datacenter_app.command("list")
@cli_errors
def datacenter_list(limit: int = LimitOption, as_json: bool = JsonOption,
                    target: TargetOption = None) -> None:
    """Data centers with status and compatibility version."""
    conn, _ = get_connection(target)
    out = inventory.list_datacenters(conn, limit=limit)
    if as_json:
        console.print_json(json.dumps(out))
        return
    _table("Data centers", ["name", "status", "compatibilityVersion", "local", "id"],
           out["dataCenters"])
    _footer(out)


@cluster_app.command("list")
@cli_errors
def cluster_list(limit: int = LimitOption, as_json: bool = JsonOption,
                 target: TargetOption = None) -> None:
    """Clusters with compatibility version and CPU type."""
    conn, _ = get_connection(target)
    out = inventory.list_clusters(conn, limit=limit)
    if as_json:
        console.print_json(json.dumps(out))
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
    conn, _ = get_connection(target)
    out = inventory.list_hosts(conn, limit=limit, search=search)
    if as_json:
        console.print_json(json.dumps(out))
        return
    _table("Hosts", ["name", "status", "statusDetail", "spmStatus", "vmsActive", "address"],
           out["hosts"])
    _footer(out)


@host_app.command("get")
@cli_errors
def host_get(host_id: str = typer.Argument(..., help="Host id (see 'host list')."),
             target: TargetOption = None) -> None:
    """One host, as JSON."""
    conn, _ = get_connection(target)
    console.print_json(json.dumps(inventory.get_host(conn, host_id)))


@host_app.command("health")
@cli_errors
def host_health(events_limit: int = typer.Option(200, "--events-limit",
                                                 help="Recent warning+ events to correlate."),
                as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """What needs attention on hosts, worst first."""
    conn, _ = get_connection(target)
    out = diagnose.host_health_rca(conn, events_limit=events_limit)
    if as_json:
        console.print_json(json.dumps(out))
        return
    counts = ", ".join(f"{k}={v}" for k, v in sorted(out["hostStatusCounts"].items())) or "none"
    console.print(f"{out['hostsEvaluated']} host(s): {counts}")
    if not out["findings"]:
        console.print("[green]No findings.[/]")
    for f in out["findings"]:
        colour = {"critical": "red", "high": "red", "medium": "yellow"}.get(f["severity"], "dim")
        # The severity label is bracketed; unescaped, rich reads "[info]" as markup and drops it.
        label = escape(f"[{f['severity']}]")
        console.print(f"[{colour}]{f['rank']}. {label} {escape(f['host'] or '-')}: "
                      f"{escape(f['signal'])}[/]")
        console.print(f"   cause: {escape(f['cause'])}\n   action: {escape(f['action'])}")
    if out["hostsTruncated"] or out["eventsTruncated"]:
        console.print("[yellow]PARTIAL: the host or event scan was cut short.[/]")
