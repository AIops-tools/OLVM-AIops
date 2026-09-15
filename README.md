<!-- mcp-name: io.github.AIops-tools/olvm-aiops -->

# OLVM AIops

> **Disclaimer**: Community-maintained open-source project. **Not affiliated with, endorsed by, or sponsored by Oracle or the oVirt project.** "Oracle", "Oracle Linux" and "oVirt" are trademarks of their owners. MIT licensed.

AI-powered operations for **Oracle Linux Virtualization Manager (OLVM)** and
**oVirt 4.5**, over the engine REST API, with a **built-in governance harness** —
an audit log that MCP and CLI calls both write to, a token/runaway budget guard,
and descriptive risk tiers. Built for teams that run OLVM (often next to another
hypervisor platform) and want an agent to answer "what needs attention?" with
evidence from the engine, not guesses. Built on `httpx` and the MCP SDK, not on the
pycurl-based engine SDK.

> **Read-only in this release**: inventory, health, capacity and diagnosis.
> Engine actions are asynchronous — the engine answers `complete` long before a
> VM or host reaches its target state — so write tools are held back until each
> can confirm its own outcome. Do NOT use for XCP-ng — use xcpng-aiops. Do NOT
> use for Proxmox VE — use proxmox-aiops.

## What this tool does, and does not, decide

It reads an OLVM / oVirt engine accurately and records every call. It does
**not** decide what an agent may change — that belongs to the engine account you
connect it with. Give that account a read-only role (such as `ReadOnlyAdmin`) and
the engine itself enforces it.

The one thing the tool guarantees is that nothing is silent: **every call, over
MCP and over the CLI alike, lands an audit row** in `~/.olvm-aiops/audit.db`.

Running a smaller / local model? See
[agent-guardrails.md](skills/olvm-aiops/references/agent-guardrails.md) for what
the tool already guarantees and a ready-made system prompt for the rest.

## What it answers

| Question | Tool | CLI |
|---|---|---|
| Is the engine itself healthy — certificates, backups, clock? | `engine_health_rca` | `olvm-aiops engine health` |
| What is wrong with my hosts? | `host_health_rca` | `olvm-aiops host health` |
| Is storage about to stop the engine creating disks? | `storage_capacity_rca` | `olvm-aiops storage capacity` |
| Why is this VM paused / not responding? | `vm_health_rca` | `olvm-aiops vm health` |
| What happened recently? What is new since I last looked? | `event_list` | `olvm-aiops event list` |
| What long-running operations failed? | `job_list` | `olvm-aiops job list --status failed` |
| Inventory | `datacenter_list`, `cluster_list`, `host_list`/`host_get`, `storage_domain_list`/`storage_domain_get`, `vm_list`/`vm_get`, `vm_stats` | `datacenter`, `cluster`, `host`, `storage`, `vm` |

**17 MCP tools**: 15 reads and diagnoses, plus the harness's `undo_list` /
`undo_apply` (which have nothing to undo in a read-only release).

Each diagnosis ranks findings worst first; every finding carries the measured
`signal`, a `cause`, an `action` and an explicit `rank`. Transient states are not
reported as failures: a host the engine is installing or rebooting is "in
progress", alert 9000 on a host without fencing hardware is informational, and an
event the host, VM or data center has since recovered from is marked superseded.
Events older than 24 hours (`events_window_hours`) are history, not findings, and
every warning-or-worse event inside the window lands in exactly one diagnosis —
engine-wide alerts such as certificate expiry and missing backups included.

## Built from a live engine, not from the docs

Every read was written against, and every fixture captured from, a live
**Oracle Linux Virtualization Manager 4.5.5-1.73.el9** engine with Keycloak
enabled. Things that engine does, which this tool accounts for:

- counts, sizes and flags arrive as JSON strings, timestamps as epoch-ms numbers;
- an attached storage domain has **no status** in `/storagedomains` — status is read
  from each data center's storage-domain collection;
- `/jobs` refuses any `search` and returns jobs oldest first, so jobs are sorted here;
- the `time` search on events is unusable (formats return all or nothing), while
  `from=<index>` is an exact cursor — hence `after_index` and a client-side `since_minutes`;
- the cursor sorted newest first returns the newest events above it and skips the rest, so
  `after_index` reads oldest first;
- `/ovirt-engine/services/health` answers without a login, and a data center's old status
  alerts stay in the log after it recovers;
- login events print the SSO session id, which is redacted before events are returned.

## Quick start

### As a Claude Code plugin

One install gives an agent both the skill and the MCP server:

```
/plugin marketplace add AIops-tools/marketplace
/plugin install olvm-aiops@aiops-tools
```

The MCP server is fetched with [uv](https://docs.astral.sh/uv/) and pinned to the
package version this plugin declares, so an audit row can be traced back to the
code that wrote it. Credentials are still configured with `olvm-aiops init` — see below.

### As an OpenClaw plugin

The same bundle is published on [ClawHub](https://clawhub.ai/plugins), where one
install delivers the skill and its MCP server together:

```bash
openclaw plugins install clawhub:@zw008/olvm-aiops
openclaw skills info olvm-aiops          # expect: Visible to model: yes
```

Restart the OpenClaw gateway afterwards so it loads the plugin. The MCP server is
fetched with [uv](https://docs.astral.sh/uv/), pinned to this exact release, so
`uvx` has to be on `PATH` — without it the skill still installs but reports
`Visible to model: no`. Credentials are configured exactly as below.

### As a CLI or standalone MCP server

```bash
uv tool install olvm-aiops
olvm-aiops init        # wizard: engine URL, username with profile, CA file, encrypted password
olvm-aiops doctor      # verify config, encrypted store, login and engine version
olvm-aiops host health # first diagnosis
```

`init` writes `~/.olvm-aiops/config.yaml` (non-secret connection details) and
stores the password **encrypted** in `~/.olvm-aiops/secrets.enc`. Example:

```yaml
targets:
  - name: engine1
    url: https://olvm-engine.example.com     # the Administration Portal origin (FQDN)
    username: admin@ovirt@internalsso        # admin@internal on engines without Keycloak
    ca_file: /etc/pki/olvm-engine-ca.pem     # engine CA; keeps verify_ssl on
    timeout: 30                              # seconds per request; raise for busy engines
```

The username includes its profile. Engine-setup enables Keycloak by default since
4.5.1, which makes the admin `admin@ovirt@internalsso`; engines without Keycloak use
`admin@internal`. Download the engine CA from
`https://<engine>/ovirt-engine/services/pki-resource?resource=ca-certificate&format=X509-PEM-CA`
and connect by FQDN — the engine certificate does not cover its IP address.

For non-interactive use (MCP server, CI, cron) export the master password so the
store can be unlocked without a prompt:

```bash
export OLVM_AIOPS_MASTER_PASSWORD='your-master-password'
```

> **Where that password then lives**: an exported variable is readable by
> every process this shell starts and is recorded by shell history. On a
> shared or long-lived host, prefer the interactive prompt, or inject it from
> a secret manager for the life of the one command that needs it.

### MCP client config

```json
{
  "mcpServers": {
    "olvm-aiops": {
      "command": "uvx",
      "args": ["--from", "olvm-aiops", "olvm-aiops-mcp"],
      "env": { "OLVM_AIOPS_MASTER_PASSWORD": "your-master-password" }
    }
  }
}
```

> **Env-block caveat**: MCP clients launch the server with a minimal
> environment — your shell profile's exports are **not** inherited. Put
> `OLVM_AIOPS_MASTER_PASSWORD` (and, if you use them, `OLVM_AIOPS_HOME` /
> `OLVM_AIOPS_CONFIG` / `OLVM_AUDIT_APPROVED_BY`) in the `env` block above,
> or the encrypted store cannot be unlocked and every tool returns a teaching
> error.

### Managing secrets

```bash
olvm-aiops secret set engine1          # prompts hidden for the account password
olvm-aiops secret list                 # names only, values never shown
olvm-aiops secret rm engine1
olvm-aiops secret rotate-password      # re-encrypt under a new master password
olvm-aiops secret migrate              # import a legacy plaintext .env, then retires it
```

A legacy plaintext env var `OLVM_<TARGET_NAME_UPPER>_PASSWORD` is still honoured
as a fallback with a deprecation warning (migrate with `olvm-aiops secret migrate`).

## Governance

Every MCP tool passes through `@governed_tool`, and every CLI command calls the MCP tool of the
same name. It records; it does not authorize.

- **Audit** — every call (tool, params with secrets redacted, result, status, duration, risk tier, and any operator-supplied approver/rationale) lands in `~/.olvm-aiops/audit.db` (relocate with `OLVM_AIOPS_HOME`).
- **Budget / runaway guard** — a safety backstop, not an authorization gate: cumulative call and wall-time caps plus a tight-loop circuit breaker (`OLVM_MAX_TOOL_CALLS`, `OLVM_MAX_TOOL_SECONDS`, `OLVM_RUNAWAY_MAX`).
- **Risk tier** — a descriptive label on the audit row derived from `risk_level`; it gates nothing.
- **Output hygiene** — all engine-returned text is sanitized and bounded before it reaches the agent; SSO session ids in login events are redacted.

## 支持范围 / Supported scope

| Area | Read | Write |
|------|------|-------|
| Engine | health diagnosis (health check, clock, certificates, backups) | — |
| Hosts | list / get / health diagnosis | — |
| Storage domains | list / get (data-center-scoped status, capacity) / capacity diagnosis | — |
| VMs | list / get / statistics / health diagnosis | — |
| Data centers, clusters | list | — |
| Events, jobs | list (severity, paging, `after_index`, `since_minutes`; job status) | — |

**缺功能？(Missing something?)** Coverage is intentionally focused. Open an issue or PR at
[github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops/issues)
— feature requests, contributions, and comments are all welcome.

## Scope & caveats

- **Verification status**: every read and all four diagnoses were run end to end
  against a live OLVM 4.5.5 engine with one KVM host, an NFS data domain and one VM.
  Not yet verified: production-scale engines, iSCSI / FC / Gluster domains, multi-host
  clusters, self-hosted engine deployments, or engines without Keycloak. See
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md).
- **Engine only**: no direct host (vdsm) access. Hosts, storage and VMs are seen the
  way the engine sees them.
- **No writes** in this release. Start/stop/migrate, maintenance and snapshots are
  planned once each write can confirm its own outcome rather than the engine's
  immediate `complete`.

## Not for

XCP-ng (use xcpng-aiops), Proxmox VE (use proxmox-aiops), other hypervisors,
NAS/storage appliances, backup suites, container clusters, or network devices.

## License

MIT — [github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops)
