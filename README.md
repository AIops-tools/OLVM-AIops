<!-- mcp-name: io.github.AIops-tools/olvm-aiops -->

# XCP-ng AIops

> **Disclaimer**: Community-maintained open-source project. **Not affiliated with, endorsed by, or sponsored by Vates, the XCP-ng project, or the Xen Orchestra project.** "XCP-ng", "Xen Orchestra", and "Xen" are trademarks of their owners. MIT licensed.

AI-powered **XCP-ng** operations **via Xen Orchestra's REST API** with a
**built-in governance harness** — unified audit log, policy engine,
token/runaway budget guard, undo-token recording, and descriptive risk
tiers. Built for homelabs and small/self-hosted XCP-ng fleets that want an AI
agent to triage VM health, storage pressure, backup failures, and patch
posture — with every write audited, previewable, and (where honest)
reversible. Self-contained: no dependencies beyond `httpx` and the MCP SDK.

> **Requires a Xen Orchestra instance** (XO from sources or the Xen Orchestra
> Appliance, 5.x with `/rest/v0`). XO is the management plane this tool talks
> to — **direct per-host XAPI access is out of scope for v0.1**. Do NOT use
> for Proxmox VE — use proxmox-aiops.

## What works

- **CLI** (`olvm-aiops ...`): `init`, `overview`, `vm list/get/stats/health-rca/start/stop/reboot/migrate`, `host list/get/missing-patches`, `pool list/get/posture`, `sr list/get/vdis/usage-rca/rescan`, `snapshot list/create/delete/revert`, `backup jobs/logs/failure-rca`, `task list`, `secret set/list/rm/migrate/rotate-password`, `doctor`, `mcp`.
- **MCP server** (`olvm-aiops mcp` or `olvm-aiops-mcp`): **29 tools** (19 read, 8 write, 2 undo), every one wrapped with the bundled `@governed_tool` harness.
- **Four flagship RCA analyses** (cause + action structured output): VM health, SR usage, backup-job failures, pool patch & HA posture.
- **Encrypted credentials**: the XO authentication token lives in an encrypted store `~/.olvm-aiops/secrets.enc` (Fernet + scrypt) — **never plaintext on disk**. Unlock with a master password from `OLVM_AIOPS_MASTER_PASSWORD` (MCP/CI) or an interactive prompt (CLI).
- **Reversibility**: `vm_start` ↔ `vm_stop` record each other as inverses; `vm_migrate` captures the REAL source host before moving and records "migrate back"; `snapshot_create` captures the created snapshot's REAL id from the XO response and records "delete THAT snapshot". Irreversible ops (`snapshot_delete`, `snapshot_revert`, `vm_reboot`) capture prior state for the audit record and honestly declare **no undo**.
- **Safety**: destructive CLI ops require double confirmation and support `--dry-run`; every write MCP tool takes a `dry_run` preview (no write call, no undo recorded).
- **Self-lockout guard (partial — read this)**: Xen Orchestra is commonly a VM on a pool it manages, and stopping that VM kills the API this tool talks to — `vm_start` can then no longer be sent, so recovery needs hypervisor console access (`xe vm-start`). Set `xo_self_vm_uuid` on the target (`olvm-aiops init` asks) and `vm_stop` refuses exactly that uuid — on `--dry-run` as well, since a preview that green-lights a call the tool will then refuse is reporting the wrong outcome. **If you do not set it there is no protection at all**: XO's REST API exposes no self endpoint and its token carries no claims, so the tool cannot discover which VM it runs on, and it fails open rather than guess. The `dry_run` preview adds a weaker `selfVmHint` when a VM's reported IP matches the configured XO host — that is a coincidence worth checking, not a finding, and it never blocks (it sees nothing without the guest agent and fires on every VM behind a shared proxy).

## Capability matrix (29 MCP tools)

| Domain | Tools | Count | R/W |
|--------|-------|:-----:|:---:|
| **Overview** | `overview` | 1 | read |
| **VMs** | `vm_list`, `vm_get`, `vm_stats`, `vm_health_rca` | 4 | read |
| | `vm_start`, `vm_stop`, `vm_reboot`, `vm_migrate` | 4 | write (medium) |
| **Hosts** | `host_list`, `host_get` | 2 | read |
| **Pools** | `pool_list`, `pool_get`, `pool_patch_ha_posture` | 3 | read |
| **SRs / VDIs** | `sr_list`, `sr_get`, `vdi_list`, `sr_usage_rca` | 4 | read |
| | `sr_rescan` | 1 | write (medium) |
| **Snapshots** | `snapshot_list` | 1 | read |
| | `snapshot_create` (medium), `snapshot_delete` (high), `snapshot_revert` (high) | 3 | write |
| **Backups** | `backup_job_list`, `backup_log_list`, `backup_failure_rca` | 3 | read |
| **Tasks** | `task_list` | 1 | read |
| **Undo** | `undo_list`, `undo_apply` | 2 | read + replay |

### Flagship RCAs

1. **`vm_health_rca`** — VMs halted unexpectedly (auto-poweron / HA restart priority set), paused/suspended VMs, running VMs without guest tools, CPU/memory pressure from RRD stats → cause + action per finding.
2. **`sr_usage_rca`** — SRs ranked by physical fullness (near-full ≥ 85%, critical ≥ 95%), thin-provision overcommit (virtual allocation > capacity), orphaned VDIs (attached to no VM) with reclaimable bytes per SR.
3. **`backup_failure_rca`** — failed/skipped/interrupted XO backup runs classified: **vdi-chain** (coalesce not finished), **quiesce** (guest VSS), **transport** (remote unreachable), **storage-full**, unknown — with per-job counts and sample messages.
4. **`pool_patch_ha_posture`** — hosts missing patches, hosts pending reboot, **version skew** across a pool's hosts (breaks live migration / rolling updates), multi-host pools without HA.

## What this tool does, and does not, decide

It delivers XCP-ng operations — reads and writes — accurately and efficiently,
and records every one of them. It does **not** decide whether a write is allowed
to happen. That is the agent's judgement, or the permission of the Xen Orchestra
account whose token you connect it with: give that XO user a read-only ACL, or
scope its token down, and the writes fail at Xen Orchestra — the place that
actually owns the permission.

So there is no read-only switch, no policy file, no approval gate to configure.
The one thing the tool guarantees is that nothing is silent: **every call, over
MCP and over the CLI alike, lands an audit row** in `~/.olvm-aiops/audit.db`,
and reversible writes still capture their before-state and record an inverse.

> Each tool declares a `risk_level`, kept in agreement with its `[READ]`/`[WRITE]`
> documentation tag by a test, and carried into the audit row as a descriptive
> tier — so a reviewer can see at a glance that a row was a high-risk snapshot
> delete. It is a label, not a gate.

Running a smaller / local model? See
[agent-guardrails.md](skills/olvm-aiops/references/agent-guardrails.md) — it lists
the guardrails this tool now enforces for you (so you don't spend prompt budget
restating them) and gives a ready-made system prompt for what's left.

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
olvm-aiops init        # interactive wizard: XO URL + encrypted token
olvm-aiops doctor      # verify config, encrypted store, XO reachability + pool count
olvm-aiops overview    # one-shot fleet health summary
```

`init` writes `~/.olvm-aiops/config.yaml` (non-secret connection details) and
stores the XO token **encrypted** in `~/.olvm-aiops/secrets.enc`. Example
config it produces:

```yaml
targets:
  - name: xo1
    url: https://xo.example.com   # the XO web origin (management plane)
    verify_ssl: true              # set false only for self-signed lab certs
    api_path: /rest/v0
```

Create the token in the XO UI (**user menu → Personal tokens**) or with
`xo-cli --createToken`. For non-interactive use (MCP server, CI, cron) export
the master password so the store can be unlocked without a prompt:

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
olvm-aiops secret set xo1              # prompts hidden for the XO token
olvm-aiops secret list                 # names only, values never shown
olvm-aiops secret rm xo1
olvm-aiops secret rotate-password      # re-encrypt under a new master password
olvm-aiops secret migrate              # import a legacy plaintext .env, then retires it
```

A legacy plaintext env var `OLVM_<TARGET_NAME_UPPER>_TOKEN` is still honoured
as a fallback with a deprecation warning (migrate with `olvm-aiops secret migrate`).

## Governance

Every MCP tool — and every CLI write, which routes through the same governed
functions — passes through `@governed_tool`. It records; it does not authorize
(see above).

- **Audit** — every call (tool, params with secrets redacted, result, status, duration, risk tier, and any operator-supplied approver/rationale) lands in `~/.olvm-aiops/audit.db` (relocate with `OLVM_AIOPS_HOME`). The CLI writes the same row the MCP path does — there is no unaudited entry point.
- **Budget / runaway guard** — a safety backstop, not an authorization gate: cumulative call and wall-time caps plus a tight-loop circuit breaker (`OLVM_MAX_TOOL_CALLS`, `OLVM_MAX_TOOL_SECONDS`, `OLVM_RUNAWAY_MAX`) stop a stuck agent from burning unbounded calls/time.
- **Undo recording** — reversible writes record a replayable inverse descriptor to `~/.olvm-aiops/undo.db` and return an `_undo_id`; irreversible writes record prior state only.
- **Risk tier** — a descriptive label on the audit row derived from `risk_level`; it gates nothing.
- **Output hygiene** — all XO-returned text is sanitized and bounded before it reaches the agent.

## 支持范围 / Supported scope

| Area | Read | Write (governed) |
|------|------|------------------|
| VMs | list / get / RRD stats / health RCA | start, stop (clean/hard), reboot (clean/hard), migrate |
| Hosts | list / get / missing patches | — |
| Pools | list / get / patch & HA posture RCA | — |
| SRs / VDIs | list / get / VDI list (orphan filter) / usage RCA | rescan |
| Snapshots | list | create, delete, revert |
| Backups | jobs / logs / failure RCA | — |
| Tasks | list | — |

**缺功能？(Missing something?)** Coverage is intentionally focused. Open an issue or PR at
[github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops/issues)
— feature requests, contributions, and comments are all welcome.

## Scope & caveats

- **Verification status**: all behaviour is validated against mocked REST
  responses; there is no recorded end-to-end run against a live Xen Orchestra
  instance yet. `olvm-aiops doctor` is the fastest live check — see
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md) for the full checklist.
- Endpoint paths (e.g. `/vms/<id>/actions/snapshot`, `/vm-snapshots/<id>`,
  `/srs/<id>/actions/rescan`, `/hosts/<id>/missing_patches`, `/backup/logs`)
  are modelled against the documented XO REST `/rest/v0` API and need live
  verification — action names may differ across XO releases.
- **Management plane only**: everything goes through XO. Per-host XAPI,
  XO server management (adding servers, users), and backup job *execution*
  (run/restore) are out of scope for v0.1.
- Out of scope by design: anything that destroys bulk data (VM/VDI deletion) —
  only `snapshot_delete` / `snapshot_revert` discard state, and both are
  `high` risk + double-confirmed.

## Not for

Other hypervisors or VM platforms (use their own ops tools — e.g. Proxmox VE →
proxmox-aiops), NAS/storage appliances, backup software suites, container
clusters, or network devices — those are out of scope for this tool.

## License

MIT — [github.com/AIops-tools/OLVM-AIops](https://github.com/AIops-tools/OLVM-AIops)
