"""Shared helpers for olvm-aiops CLI sub-modules."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

console = Console()

# ─── Shared Option types ───────────────────────────────────────────────────

TargetOption = Annotated[
    str | None, typer.Option("--target", "-t", help="Target name from config")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Print the API call without executing")
]


def _cli_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions translated to a one-line teaching error instead of a traceback.

    ``BudgetExceeded`` (and the retained ``PolicyDenied`` type) are raised by
    ``@governed_tool`` OUTSIDE the tool body, so ``tool_errors`` never sees them
    and they never arrive as an ``{"error": ...}`` dict. Their message is the
    teaching text (e.g. which budget was hit) — without them here such a refusal
    reaches the CLI as a traceback instead.
    """
    from olvm_aiops.connection import OlvmApiError
    from olvm_aiops.governance import BudgetExceeded, PolicyDenied
    from olvm_aiops.secretstore import SecretStoreError

    return (OlvmApiError, PolicyDenied, BudgetExceeded, SecretStoreError, KeyError, OSError,
            ValueError)


def cli_errors(fn: Callable) -> Callable:
    """Translate known exceptions into one red line + exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except _cli_error_types() as e:
            message = str(e)
            if isinstance(e, KeyError):
                message = f"Missing required key or environment variable: {message}"
            # Engine text can contain "[/bold]"; unescaped, rich raises on it and the
            # real error is replaced by a MarkupError traceback.
            console.print(f"[red]Error: {escape(message)}[/]")
            raise typer.Exit(1) from e

    return wrapper


#: Exit status for a write whose outcome could not be determined — kept distinct
#: from 0 (confirmed) and 1 (failed) so a script can tell all three apart.
EXIT_UNDETERMINED = 2


def governed(result: Any) -> dict:
    """Return a governed tool's result, or print its error and exit 1.

    The ``mcp_server.tools`` twins never raise: ``@tool_errors`` flattens every
    failure — an unreachable engine, a refused login, a bad argument — into
    ``{"error": ...}``. A CLI command that drops that on the floor prints an empty
    table and exits 0 as if the engine had nothing to report. Route every governed
    call through here.
    """
    # ``outcomeUnknown`` is judged BEFORE ``error``, matching the harness: a
    # write whose response was lost carries BOTH keys, and it is audited
    # `unknown` precisely because it may have taken effect. Reporting that as a
    # plain failure would tell a script the change did not happen and invite the
    # double-apply the payload's own note warns about.
    if isinstance(result, dict) and result.get("outcomeUnknown"):
        console.print(f"[yellow]Outcome undetermined: {escape(str(result.get('note') or ''))}[/]")
        raise typer.Exit(EXIT_UNDETERMINED)
    if isinstance(result, dict) and result.get("error"):
        console.print(f"[red]Error: {escape(str(result['error']))}[/]")
        raise typer.Exit(1)
    return result if isinstance(result, dict) else {}


def dry_run_print(*, operation: str, api_call: str, parameters: dict | None = None) -> None:
    """Print a dry-run preview of the API call that would be made."""
    console.print("\n[bold magenta][DRY-RUN] No changes will be made.[/]")
    console.print(f"[magenta]  Operation: {operation}[/]")
    console.print(f"[magenta]  API Call:  {api_call}[/]")
    for k, v in (parameters or {}).items():
        console.print(f"[magenta]  Param:     {k} = {v}[/]")
    console.print("[magenta]  Run without --dry-run to execute.[/]\n")


def double_confirm(action: str, resource: str) -> None:
    """Require two confirmations for a destructive operation."""
    console.print(f"[bold yellow]⚠️  About to: {action} '{resource}'[/]")
    typer.confirm(f"Confirm 1/2: {action} '{resource}'?", abort=True)
    typer.confirm(
        f"Confirm 2/2: really {action} '{resource}'? This may be irreversible.",
        abort=True,
    )


def print_truncation_note(result: dict, noun: str) -> None:
    """Print a visible warning when a listing envelope was capped.

    The JSON already carries ``truncated``, but a human scrolling a long dump
    will not spot it — and neither will a model summarising the terminal. Say
    it in words, on the last line, where it cannot be missed.
    """
    if isinstance(result, dict) and result.get("truncated"):
        console.print(
            f"[yellow]… showing {result.get('returned')} of more {noun} — "
            f"truncated, re-run with a higher --limit[/]"
        )
