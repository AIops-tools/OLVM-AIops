# Security Policy

## Disclaimer

Community-maintained open-source project. **Not affiliated with, endorsed by, or
sponsored by Vates, the XCP-ng project, or the Xen Orchestra project.**
"XCP-ng", "Xen Orchestra", and "Xen" are trademarks of their owners. Source is
publicly auditable under the MIT license.

## Reporting Vulnerabilities

Report privately via a GitHub Security Advisory on
[github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops/security/advisories)
or email zhouwei008@gmail.com. Please do not open public issues for security
reports.

## Security Design

### Credential Management
- Per-target Xen Orchestra authentication tokens live **encrypted** in
  `~/.olvm-aiops/secrets.enc` (Fernet/AES-128 + scrypt-derived key; chmod
  600), never in `config.yaml` and never in source. The master password is
  never stored — only a per-store random salt and the ciphertext are on disk.
- A legacy plaintext env var `OLVM_<TARGET_NAME_UPPER>_TOKEN` is still
  honoured as a fallback with a deprecation warning (migrate with
  `olvm-aiops secret migrate`).
- The token is sent in request headers (`Authorization: Bearer` and the
  `authenticationToken` cookie) at request time and held only in memory.
  Tokens are never logged or echoed; the config file holds only the XO URL,
  api_path, and TLS settings.

### Governed Operations
Every MCP tool runs through the bundled `@governed_tool` harness
(`olvm_aiops.governance`):
- **Audit** — every call logged to a local SQLite DB under `~/.olvm-aiops/`
  (relocatable via `OLVM_AIOPS_HOME`), agent-attributed, secret-redacted.
- **Token/runaway budget** — hard ceilings (`OLVM_MAX_TOOL_CALLS` /
  `OLVM_MAX_TOOL_SECONDS`) plus an on-by-default guard that trips a tight
  poll/retry loop, preventing unbounded API consumption (e.g. polling a slow
  task).
- **Risk tier** — a descriptive label on each audit row derived from
  `risk_level`; it gates nothing. `OLVM_AUDIT_APPROVED_BY` /
  `OLVM_AUDIT_RATIONALE` are optional annotations recorded on the row, never
  required and never blocking.
- **Undo-token recording** — `vm_start`/`vm_stop` record each other as
  inverses, `vm_migrate` records migrating back to the captured source host,
  and `snapshot_create` records deleting the REAL snapshot id XO returned.

### Destructive Operations
`snapshot delete`, `snapshot revert`, `vm stop`, `vm reboot`, and `vm migrate`
require double confirmation at the CLI layer and support `--dry-run`; the MCP
twins take a `dry_run` preview. Snapshot delete/revert are irreversible
(state loss), tagged `risk_level=high`, capture the BEFORE state for the audit
record, and record no undo token.

### SSL/TLS Verification
`verify_ssl` defaults to true; disable only for self-signed lab certificates.

### Output Hygiene
All XO-API-returned text (VM/SR/host names, log messages, descriptions) is
passed through a `sanitize()` truncate + control-character strip before
reaching the agent.

### Network Scope
No webhooks, no telemetry, no outbound calls beyond the configured Xen
Orchestra REST API endpoint. No post-install scripts or background services.

## Static Analysis

```bash
uvx bandit -r olvm_aiops/ mcp_server/
uv run ruff check .
```

## Supported Versions

The latest released version receives security fixes. This is a preview (0.x);
pin a version in production.
