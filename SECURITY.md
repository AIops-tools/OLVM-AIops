# Security Policy

## Disclaimer

Community-maintained open-source project. **Not affiliated with, endorsed by, or
sponsored by Oracle or the oVirt project.** "Oracle", "Oracle Linux" and "oVirt"
are trademarks of their owners. Source is publicly auditable under the MIT license.

## Reporting Vulnerabilities

Report privately via a GitHub Security Advisory on
[github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops/security/advisories)
or email zhouwei008@gmail.com. Please do not open public issues for security
reports.

## Security Design

### Credential Management
- Per-target engine account passwords live **encrypted** in
  `~/.olvm-aiops/secrets.enc` (Fernet/AES-128 + scrypt-derived key; chmod 600),
  never in `config.yaml` and never in source. The master password is never
  stored — only a per-store random salt and the ciphertext are on disk.
- A legacy plaintext env var `OLVM_<TARGET_NAME_UPPER>_PASSWORD` is still
  honoured as a fallback with a deprecation warning (migrate with
  `olvm-aiops secret migrate`).
- The password is exchanged once for an SSO access token
  (`/ovirt-engine/sso/oauth/token`); the token is held only in memory, sent as a
  Bearer header, and revoked (`/ovirt-engine/services/sso-logout`) when the
  connection closes. Neither is logged or echoed.
- **Least privilege is the authorization boundary.** This tool does not decide
  what an agent may change; the engine does. Give it an account whose role only
  permits what you want done (for read-only use, a user with a read-only role
  such as `ReadOnlyAdmin`), and writes are refused by the engine itself.

### Governed Operations
Every MCP tool runs through the bundled `@governed_tool` harness
(`olvm_aiops.governance`):
- **Audit** — every call logged to a local SQLite DB under `~/.olvm-aiops/`
  (relocatable via `OLVM_AIOPS_HOME`), agent-attributed, secret-redacted; the
  CLI writes the same rows as the MCP server.
- **Token/runaway budget** — hard ceilings (`OLVM_MAX_TOOL_CALLS` /
  `OLVM_MAX_TOOL_SECONDS`) plus an on-by-default guard that trips a tight
  poll/retry loop.
- **Risk tier** — a descriptive label on each audit row derived from
  `risk_level`; it gates nothing. `OLVM_AUDIT_APPROVED_BY` /
  `OLVM_AUDIT_RATIONALE` are optional annotations, never required.

### Scope of v0.1
Read-only: inventory, health, capacity and diagnosis. No tool in this release
changes engine state. Write operations are deferred until the engine's
asynchronous action semantics (jobs and correlation ids) are verified on a live
engine, so a submitted action is never reported as a completed one.

### SSL/TLS Verification
`verify_ssl` defaults to true. Point `ca_file` at the engine CA
(`https://<engine>/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA`)
to verify the engine's certificate; disable verification only for throwaway lab
engines.

### Output Hygiene
All engine-returned text (names, descriptions, event messages, fault details)
passes through `sanitize()` (truncate + control-character strip) before
reaching the agent.

### Network Scope
No webhooks, no telemetry, no outbound calls beyond the configured engine URL.
No post-install scripts or background services.

## Static Analysis

```bash
uvx bandit -r olvm_aiops/ mcp_server/
uv run ruff check .
```

## Supported Versions

The latest released version receives security fixes. This is 0.x; pin a version
in production.
