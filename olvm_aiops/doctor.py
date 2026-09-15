"""Environment and connectivity diagnostics for OLVM AIops.

Checks: config + encrypted secret store, then per target — engine reachability,
SSO login (a wrong username profile or password surfaces as a failed check),
and the engine's own product version.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from olvm_aiops.config import CONFIG_FILE, ENV_FILE, load_config
from olvm_aiops.secretstore import SECRETS_FILE, SecretStoreError, check_permissions, has_store

_console = Console()


def _version_text(root: dict) -> str:
    info = root.get("product_info") if isinstance(root, dict) else None
    if not isinstance(info, dict):
        return "version not reported"
    name = info.get("name") or "engine"
    version = info.get("version") if isinstance(info.get("version"), dict) else {}
    full = version.get("full_version")
    # Engine-supplied text is printed inside rich markup: escape it.
    return escape(f"{name} {full}" if full else str(name))


def run_doctor(skip_auth: bool = False) -> int:
    """Check config, secrets, and (optionally) connectivity.

    Returns a process exit code: 0 healthy, 1 problems found. Connectivity
    failures are reported as status, never raised as tracebacks (a doctor must
    survive the thing it diagnoses being unhealthy).
    """
    problems = 0

    if not CONFIG_FILE.exists():
        _console.print(f"[red]✗ Config file missing: {CONFIG_FILE}[/]")
        _console.print("[yellow]  Run 'olvm-aiops init' to set up your first target.[/]")
        return 1
    _console.print(f"[green]✓ Config file present: {CONFIG_FILE}[/]")

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 — report, do not crash
        _console.print(f"[red]✗ Config load failed: {escape(str(exc))}[/]")
        return 1

    if not config.targets:
        _console.print("[red]✗ No targets configured[/]")
        return 1
    _console.print(f"[green]✓ {len(config.targets)} target(s) configured[/]")

    if has_store():
        _console.print(f"[green]✓ Encrypted secret store present: {SECRETS_FILE}[/]")
        perm_warning = check_permissions()
        if perm_warning:
            _console.print(f"[yellow]! {perm_warning}[/]")
    elif ENV_FILE.exists():
        _console.print(
            f"[yellow]! Using legacy plaintext .env ({ENV_FILE}). Migrate with "
            f"'olvm-aiops secret migrate'.[/]"
        )
    else:
        _console.print(
            "[yellow]! No secret store yet. Run 'olvm-aiops init' to set up "
            "credentials (stored encrypted).[/]"
        )
        problems += 1

    for target in config.targets:
        try:
            _ = target.password
            _console.print(f"[green]✓ Password present for '{target.name}'[/]")
        except (OSError, SecretStoreError) as exc:
            _console.print(f"[red]✗ {escape(str(exc))}[/]")
            problems += 1
        if not target.verify_ssl:
            _console.print(
                f"[yellow]! '{target.name}' has verify_ssl: false — set ca_file to the "
                f"engine CA instead of disabling verification outside a lab.[/]"
            )

    if skip_auth:
        _console.print("[dim]Skipping connectivity check (--skip-auth).[/]")
        return 1 if problems else 0

    from olvm_aiops.connection import ConnectionManager

    mgr = ConnectionManager(config)
    for target in config.targets:
        try:
            conn = mgr.connect(target.name)
            root = conn.get("")
            _console.print(
                f"[green]✓ Connected to '{target.name}' ({target.url}) as "
                f"{target.username} — {_version_text(root)}[/]"
            )
        except Exception as exc:  # noqa: BLE001 — connectivity is a status, not a crash
            _console.print(f"[red]✗ Connect to '{target.name}' failed: {escape(str(exc))}[/]")
            problems += 1
    mgr.disconnect_all()

    return 1 if problems else 0
