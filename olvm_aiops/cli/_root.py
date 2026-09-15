"""Top-level Typer app: assembles sub-apps and top-level commands."""

from __future__ import annotations

import typer

from olvm_aiops.cli._common import cli_errors
from olvm_aiops.cli.doctor import doctor_cmd
from olvm_aiops.cli.init import init_cmd
from olvm_aiops.cli.secret import secret_app
from olvm_aiops.cli.undo import undo_app

app = typer.Typer(
    name="olvm-aiops",
    help="Governed Oracle Linux Virtualization Manager (OLVM) / oVirt operations.",
    no_args_is_help=True,
)

app.add_typer(secret_app, name="secret")
app.add_typer(undo_app, name="undo")
app.command("init")(init_cmd)
app.command("doctor")(doctor_cmd)


@app.command("mcp")
@cli_errors
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients (does not go through uvx/PyPI
    resolution at launch):
        olvm-aiops mcp
    """
    import sys

    if sys.version_info < (3, 11):
        typer.echo(
            f"ERROR: olvm-aiops requires Python >= 3.11 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Fix: uv python install 3.12 && "
            f"uv tool install --python 3.12 --force olvm-aiops",
            err=True,
        )
        raise typer.Exit(2)

    from mcp_server.server import main as _mcp_main

    _mcp_main()


if __name__ == "__main__":
    app()
