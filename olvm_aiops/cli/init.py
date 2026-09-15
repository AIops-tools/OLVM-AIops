"""``olvm-aiops init`` — a friendly, interactive onboarding wizard.

Walks a new user through connecting their first OLVM / oVirt engine: collects
the non-secret connection details into ``config.yaml`` and the password into
the *encrypted* store (never plaintext on disk).
"""

from __future__ import annotations

import getpass

import typer
import yaml

from olvm_aiops.cli._common import cli_errors, console
from olvm_aiops.config import CONFIG_DIR, CONFIG_FILE
from olvm_aiops.secretstore import SecretStore, resolve_master_password


def _load_existing_targets() -> list[dict]:
    if not CONFIG_FILE.exists():
        return []
    raw = yaml.safe_load(CONFIG_FILE.read_text("utf-8")) or {}
    return list(raw.get("targets", []))


def _write_targets(targets: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    CONFIG_FILE.write_text(yaml.safe_dump({"targets": targets}, sort_keys=False), "utf-8")


@cli_errors
def init_cmd() -> None:
    """Interactively set up your first OLVM / oVirt engine connection."""
    console.print("[bold cyan]OLVM AIops — setup wizard[/]")
    console.print(
        "This connects to your [bold]OLVM (or oVirt 4.5) engine[/]. It collects "
        "connection details (saved to config.yaml) and the account password "
        "(saved [bold]encrypted[/] to secrets.enc).\n"
    )

    console.print("[bold]Step 1 — master password[/]")
    console.print(
        "[dim]Encrypts secrets.enc. You'll set it via the "
        "OLVM_AIOPS_MASTER_PASSWORD env var for non-interactive/MCP use.[/]"
    )
    password = resolve_master_password(confirm_if_new=True)
    store = SecretStore.unlock(password)

    targets = _load_existing_targets()
    existing_names = {t.get("name") for t in targets}

    while True:
        console.print("\n[bold]Step 2 — add an engine target[/]")
        name = typer.prompt("Target name (e.g. engine1)").strip()
        if name in existing_names:
            if not typer.confirm(f"'{name}' already exists — overwrite?", default=False):
                continue
            targets = [t for t in targets if t.get("name") != name]

        url = typer.prompt(
            "Engine URL (the Administration Portal origin, e.g. https://engine.example.com)"
        ).strip().rstrip("/")
        console.print(
            "[dim]The username includes its profile: 'admin@internal', or "
            "'admin@ovirt@internalsso' when engine-setup enabled Keycloak (the "
            "default on new installs). A read-only account keeps writes off at the "
            "engine.[/]"
        )
        username = typer.prompt("Username", default="admin@ovirt@internalsso").strip()

        console.print(
            "[dim]To verify TLS against the engine CA, download it from "
            "https://<engine>/ovirt-engine/services/pki-resource"
            "?resource=ca-certificate&format=X509-PEM-CA "
            "and give its path. Leave blank to use the system trust store.[/]"
        )
        ca_file = typer.prompt("Engine CA file (blank for system trust)", default="",
                               show_default=False).strip()
        verify_ssl = typer.confirm(
            "Verify TLS certificate? (No only for throwaway lab engines)", default=True
        )

        secret = getpass.getpass(f"Password for {username} on '{name}' (hidden): ")
        store = store.set(name, secret)

        entry: dict = {"name": name, "url": url, "username": username,
                       "verify_ssl": verify_ssl}
        if ca_file:
            entry["ca_file"] = ca_file
        targets.append(entry)
        existing_names.add(name)
        _write_targets(targets)
        console.print(f"[green]✓ Saved target '{name}' (password stored encrypted).[/]")

        if not typer.confirm("\nAdd another target?", default=False):
            break

    console.print(f"\n[green]✓ Setup complete.[/] Config: {CONFIG_FILE}")
    console.print(
        "[dim]Tip: export OLVM_AIOPS_MASTER_PASSWORD=... in your shell profile "
        "so the MCP server and CLI can unlock secrets non-interactively.[/]"
    )
    if typer.confirm("Run a connectivity check now (olvm-aiops doctor)?", default=True):
        from olvm_aiops.doctor import run_doctor

        raise typer.Exit(run_doctor())
