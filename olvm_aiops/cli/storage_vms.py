"""CLI: storage domains and VMs (reads) plus their diagnoses."""

from __future__ import annotations

import json

import typer
from rich.markup import escape

from olvm_aiops.cli._common import TargetOption, cli_errors, console, get_connection
from olvm_aiops.cli.inventory import JsonOption, LimitOption, _footer, _table
from olvm_aiops.ops import diagnose, storage, vms

storage_app = typer.Typer(help="Storage domains: list, get, capacity diagnosis.",
                          no_args_is_help=True)
vm_app = typer.Typer(help="Virtual machines: list, get, stats, health diagnosis.",
                     no_args_is_help=True)


def _gib(value: object) -> str:
    return "-" if not isinstance(value, int) else f"{value / 1024**3:.1f}"


def _print_findings(out: dict, subject_key: str) -> None:
    if not out["findings"]:
        console.print("[green]No findings.[/]")
    for f in out["findings"]:
        colour = {"critical": "red", "high": "red", "medium": "yellow"}.get(f["severity"], "dim")
        label = escape(f"[{f['severity']}]")
        console.print(f"[{colour}]{f['rank']}. {label} {escape(f.get(subject_key) or '-')}: "
                      f"{escape(f['signal'])}[/]")
        console.print(f"   cause: {escape(f['cause'])}\n   action: {escape(f['action'])}")


@storage_app.command("list")
@cli_errors
def storage_list(limit: int = LimitOption, as_json: bool = JsonOption,
                 target: TargetOption = None) -> None:
    """Storage domains with status and capacity."""
    conn, _ = get_connection(target)
    out = storage.list_storage_domains(conn, limit=limit)
    if as_json:
        console.print_json(json.dumps(out))
        return
    rows = [{**d, "freeGiB": _gib(d["availableBytes"]), "usedGiB": _gib(d["usedBytes"])}
            for d in out["storageDomains"]]
    _table("Storage domains", ["name", "type", "status", "freeGiB", "usedGiB", "freePct"], rows)
    _footer(out)
    for err in out["statusErrors"]:
        console.print(f"[yellow]Status unreadable for data center {escape(err['dataCenterId'])}: "
                      f"{escape(err['error'])}[/]")


@storage_app.command("get")
@cli_errors
def storage_get(domain_id: str = typer.Argument(..., help="Storage domain id."),
                target: TargetOption = None) -> None:
    """One storage domain, as JSON."""
    conn, _ = get_connection(target)
    console.print_json(json.dumps(storage.get_storage_domain(conn, domain_id)))


@storage_app.command("capacity")
@cli_errors
def storage_capacity(as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """Storage-domain problems, worst first."""
    conn, _ = get_connection(target)
    out = diagnose.storage_capacity_rca(conn)
    if as_json:
        console.print_json(json.dumps(out))
        return
    console.print(f"{out['domainsEvaluated']} attached domain(s) evaluated, "
                  f"{out['domainsSkippedUnattached']} unattached skipped.")
    _print_findings(out, "storageDomain")


@vm_app.command("list")
@cli_errors
def vm_list(limit: int = LimitOption,
            search: str | None = typer.Option(None, "--search",
                                              help="Engine query, e.g. 'status=up'."),
            as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """VMs with status, host and sizing."""
    conn, _ = get_connection(target)
    out = vms.list_vms(conn, limit=limit, search=search)
    if as_json:
        console.print_json(json.dumps(out))
        return
    _table("VMs", ["name", "status", "vcpus", "memoryBytes", "highAvailability", "hostId"],
           out["vms"])
    _footer(out)


@vm_app.command("get")
@cli_errors
def vm_get(vm_id: str = typer.Argument(..., help="VM id."), target: TargetOption = None) -> None:
    """One VM, as JSON."""
    conn, _ = get_connection(target)
    console.print_json(json.dumps(vms.get_vm(conn, vm_id)))


@vm_app.command("stats")
@cli_errors
def vm_stats(vm_id: str = typer.Argument(..., help="VM id."), target: TargetOption = None) -> None:
    """A VM's current statistics, as JSON."""
    conn, _ = get_connection(target)
    console.print_json(json.dumps(vms.vm_statistics(conn, vm_id)))


@vm_app.command("health")
@cli_errors
def vm_health(events_limit: int = typer.Option(200, "--events-limit",
                                               help="Recent warning+ events to correlate."),
              as_json: bool = JsonOption, target: TargetOption = None) -> None:
    """VM problems, worst first."""
    conn, _ = get_connection(target)
    out = diagnose.vm_health_rca(conn, events_limit=events_limit)
    if as_json:
        console.print_json(json.dumps(out))
        return
    counts = ", ".join(f"{k}={v}" for k, v in sorted(out["vmStatusCounts"].items())) or "none"
    console.print(f"{out['vmsEvaluated']} VM(s): {counts}")
    _print_findings(out, "vm")
