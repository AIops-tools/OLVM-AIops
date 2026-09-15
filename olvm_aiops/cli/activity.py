"""CLI: engine events and jobs.

Every command calls the MCP tool of the same name in ``mcp_server.tools``, so it is
audited like an MCP call (see ``olvm_aiops.cli.inventory``).
"""

from __future__ import annotations

import typer

from olvm_aiops.cli._common import TargetOption, cli_errors, console, governed
from olvm_aiops.cli.inventory import JsonOption, LimitOption, _footer, _table, print_json

event_app = typer.Typer(help="Engine events.", no_args_is_help=True)
job_app = typer.Typer(help="Engine jobs (long-running operations).", no_args_is_help=True)


@event_app.command("list")
@cli_errors
def event_list(
    limit: int = LimitOption,
    min_severity: str = typer.Option("normal", "--min-severity",
                                     help="normal, warning, error or alert."),
    page: int = typer.Option(1, "--page", help="Older pages of events."),
    after_index: int | None = typer.Option(None, "--after-index",
                                           help="Only events with a higher index."),
    since_minutes: int | None = typer.Option(None, "--since-minutes",
                                             help="Only events from the last N minutes."),
    as_json: bool = JsonOption,
    target: TargetOption = None,
) -> None:
    """Engine events, newest first."""
    from mcp_server.tools import reads

    out = governed(reads.event_list(limit=limit, min_severity=min_severity, page=page,
                                    after_index=after_index, since_minutes=since_minutes,
                                    target=target))
    if as_json:
        print_json(out)
        return
    _table("Events", ["index", "time", "severity", "code", "description"], out["events"])
    _footer(out)
    if out.get("scanTruncated"):
        console.print("[yellow]PARTIAL: older events inside the window may be missing.[/]")


@job_app.command("list")
@cli_errors
def job_list(
    limit: int = LimitOption,
    status: str | None = typer.Option(None, "--status",
                                      help="started, finished, failed, aborted or unknown."),
    as_json: bool = JsonOption,
    target: TargetOption = None,
) -> None:
    """Engine jobs with status and duration."""
    from mcp_server.tools import reads

    out = governed(reads.job_list(limit=limit, status=status, target=target))
    if as_json:
        print_json(out)
        return
    _table("Jobs", ["status", "startTime", "durationSeconds", "description"], out["jobs"])
    _footer(out)
    if out.get("scanTruncated"):
        console.print("[yellow]PARTIAL: the engine holds more jobs than were read, and it lists "
                      "the newest last — the most recent jobs may be missing.[/]")
