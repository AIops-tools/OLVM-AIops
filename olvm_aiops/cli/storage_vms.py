"""CLI: storage domains and VMs (reads) plus their diagnoses.

Every command calls the MCP tool of the same name in ``olvm_aiops.mcp_server.tools``, so it is
audited like an MCP call (see ``olvm_aiops.cli.inventory``).
"""

from __future__ import annotations

import typer
from rich.markup import escape

from olvm_aiops.cli._common import TargetOption, cli_errors, console, governed
from olvm_aiops.cli.inventory import (
    EventsLimitOption,
    EventsWindowOption,
    JsonOption,
    LimitOption,
    _footer,
    _table,
    print_findings,
    print_json,
)

storage_app = typer.Typer(help="Storage domains: list, get, capacity diagnosis.",
                          no_args_is_help=True)
vm_app = typer.Typer(help="Virtual machines: list, get, stats, health diagnosis.",
                     no_args_is_help=True)


def _gib(value: object) -> str:
    return "-" if not isinstance(value, int) else f"{value / 1024**3:.1f}"


def _print_status_errors(out: dict) -> None:
    for err in out["statusErrors"]:
        console.print(f"[yellow]Status unreadable for data center {escape(err['dataCenterId'])}: "
                      f"{escape(err['error'])}[/]")


@storage_app.command("list")
@cli_errors
def storage_list(limit: int = LimitOption, as_json: bool = JsonOption,
                 target: TargetOption = None) -> None:
    """Storage domains with status and capacity."""
    from olvm_aiops.mcp_server.tools import storage_vms

    out = governed(storage_vms.storage_domain_list(limit=limit, target=target))
    if as_json:
        print_json(out)
        return
    rows = [{**d, "freeGiB": _gib(d["availableBytes"]), "usedGiB": _gib(d["usedBytes"])}
            for d in out["storageDomains"]]
    _table("Storage domains", ["name", "type", "status", "freeGiB", "usedGiB", "freePct"], rows)
    _footer(out)
    _print_status_errors(out)


@storage_app.command("get")
@cli_errors
def storage_get(domain_id: str = typer.Argument(..., help="Storage domain id."),
                target: TargetOption = None) -> None:
    """One storage domain, as JSON."""
    from olvm_aiops.mcp_server.tools import storage_vms

    print_json(governed(storage_vms.storage_domain_get(domain_id=domain_id, target=target)))


@storage_app.command("capacity")
@cli_errors
def storage_capacity(events_window_hours: int = EventsWindowOption,
                     events_limit: int = EventsLimitOption,
                     as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """Storage-domain problems, worst first."""
    from olvm_aiops.mcp_server.tools import storage_vms

    out = governed(storage_vms.storage_capacity_rca(events_limit=events_limit,
                                                    events_window_hours=events_window_hours,
                                                    target=target))
    if as_json:
        print_json(out)
        return
    console.print(f"{out['domainsEvaluated']} attached domain(s) evaluated, "
                  f"{out['domainsSkippedUnattached']} unattached skipped.")
    print_findings(out, "storageDomain")
    _print_status_errors(out)
    if out["domainsTruncated"] or out["eventsTruncated"]:
        console.print("[yellow]PARTIAL: the storage-domain or event scan was cut short.[/]")


@vm_app.command("list")
@cli_errors
def vm_list(limit: int = LimitOption,
            search: str | None = typer.Option(None, "--search",
                                              help="Engine query, e.g. 'status=up'."),
            page: int = typer.Option(1, "--page", min=1,
                                     help="One-based page number; follow nextPage."),
            as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """VMs with status, host and sizing."""
    from olvm_aiops.mcp_server.tools import storage_vms

    out = governed(storage_vms.vm_list(limit=limit, search=search, target=target, page=page))
    if as_json:
        print_json(out)
        return
    _table("VMs", ["name", "status", "vcpus", "memoryBytes", "highAvailability", "hostId"],
           out["vms"])
    if out["nextPage"] is not None:
        console.print(f"[yellow]More VMs: run with --page {out['nextPage']} "
                      f"and the same --limit/--search.[/]")


@vm_app.command("capacity")
@cli_errors
def vm_capacity(page_size: int = typer.Option(100, "--page-size", min=1, max=1000,
                                         help="VM rows per internal engine page."),
                max_pages: int = typer.Option(100, "--max-pages", min=1, max=100,
                                              help="Maximum VM pages to scan."),
                as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """Compact engine-wide VM/vCPU to host-core assignment summary."""
    from olvm_aiops.mcp_server.tools import storage_vms

    out = governed(storage_vms.vm_capacity_summary(page_size=page_size,
                                                   max_pages=max_pages, target=target))
    if as_json:
        print_json(out)
        return
    def shown(key: str) -> str:
        return str(out[key]) if out[key] is not None else "unknown"

    console.print(f"VMs: {shown('vmCount')}; configured vCPUs: {shown('configuredVcpus')}; "
                  f"physical host cores: {shown('physicalCores')}")
    ratio = out["assignmentRatioPct"]
    console.print(f"vCPU/core assignment: {ratio:.1f}%" if ratio is not None
                  else "vCPU/core assignment: unknown")
    if not out["complete"]:
        console.print(f"[yellow]PARTIAL: {escape(out['incompleteReason'])}.[/]")
    console.print("[yellow]Stateless engine pages do not provide an atomic snapshot.[/]")


@vm_app.command("get")
@cli_errors
def vm_get(vm_id: str = typer.Argument(..., help="VM id."), target: TargetOption = None) -> None:
    """One VM, as JSON."""
    from olvm_aiops.mcp_server.tools import storage_vms

    print_json(governed(storage_vms.vm_get(vm_id=vm_id, target=target)))


@vm_app.command("stats")
@cli_errors
def vm_stats(vm_id: str = typer.Argument(..., help="VM id."), target: TargetOption = None) -> None:
    """A VM's current statistics, as JSON."""
    from olvm_aiops.mcp_server.tools import storage_vms

    print_json(governed(storage_vms.vm_stats(vm_id=vm_id, target=target)))


@vm_app.command("health")
@cli_errors
def vm_health(events_window_hours: int = EventsWindowOption,
              events_limit: int = EventsLimitOption,
              as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """VM problems, worst first."""
    from olvm_aiops.mcp_server.tools import storage_vms

    out = governed(storage_vms.vm_health_rca(events_limit=events_limit,
                                             events_window_hours=events_window_hours,
                                             target=target))
    if as_json:
        print_json(out)
        return
    counts = ", ".join(f"{k}={v}" for k, v in sorted(out["vmStatusCounts"].items())) or "none"
    console.print(f"{out['vmsEvaluated']} VM(s): {escape(counts)}")
    print_findings(out, "vm")
    if out["vmsTruncated"] or out["eventsTruncated"]:
        console.print("[yellow]PARTIAL: the VM or event scan was cut short.[/]")
