# Changelog

## v0.8.2 — 2026-09-12

### Changed
- **The ClawHub bundle plugin moved from `@aiops-tools/olvm-aiops` to
  `@zw008/olvm-aiops`**, matching the publisher the skill has always been under.
  ClawHub cannot move a package between scopes — the scope is the publisher
  identity — so this is a republish under the new name; the old name is
  withdrawn. Install with:
  `openclaw plugins install clawhub:@zw008/olvm-aiops`. Nothing about the Python
  package, the CLI, the MCP server or the Claude Code plugin changes.

## v0.8.1 — 2026-09-12

### Added
- **The OpenClaw install path is documented.** The ClawHub bundle channel went
  live but neither the README nor this skill said how to install from it:
  `openclaw plugins install clawhub:@aiops-tools/olvm-aiops`. States the `uvx`
  prerequisite (without it the skill installs but reports `Visible to model:
  no`) and that the MCP server is pinned to this exact release.
- **Where an exported master password lives** is now stated next to the
  instruction to export it: readable by every process the shell starts, and
  kept in shell history.

## v0.8.0 — 2026-09-12

### Added
- **Installable as a Claude Code plugin.** `.claude-plugin/plugin.json` plus a
  root `.mcp.json` make this repo a plugin, so `/plugin install olvm-aiops@aiops-tools`
  delivers the skill and registers the MCP server in one step. The server is
  pinned to the exact package version the manifest declares, so an audit row
  stays traceable to the code that produced it. Nothing about the tool itself
  changed — the CLI and the standalone MCP server work exactly as before.
- **Installable from ClawHub as an OpenClaw bundle plugin** (`@aiops-tools/olvm-aiops`): one install delivers the skill *and* its MCP
  server, pinned to this exact release. `clawhub.ai/plugins`.

### Fixed
- **The skill was invisible to the model in OpenClaw.** Its metadata
  declared `requires.config` (OpenClaw reads that as config *keys*, not file
  paths, so it can never be satisfied), `requires.env` and `requires.bins`
  naming our own CLI — which a plugin user never has on PATH — plus a
  `primaryEnv` that turned a config path into an API-key prompt. Measured on
  OpenClaw 2026.6.35: `Visible to model: no`. It now requires
  `anyBins: [olvm-aiops, uvx]` — either one suffices — with every variable kept
  in `optional.env` (still declared, no longer a load gate), which the same
  command reports as `Visible to model: yes`.
## v0.7.0 — 2026-08-10

### Fixed
- **An undetermined outcome no longer exits as a plain failure.** A write whose response was lost carries *both* `error` and `outcomeUnknown`, and the harness deliberately judges unknown first when writing the audit row — the change may have taken effect, so a blind retry could apply it twice. The CLI guard judged `error` first, so the audit said "may have taken effect" while the exit status told a script it had not happened. The two layers now agree (exit 2, not 1), and a test pins the ordering so it cannot silently flip back.

## v0.6.0 — 2026-08-03

### Fixed
- **`undo apply` replays against the target the original write ran on.** It dispatched the inverse against whatever target the *caller* named — in practice the config's first entry — while the write's own target sat unused in the undo record. On a multi-target config the inverse therefore ran against the wrong host; it only looks harmless because the resource usually is not there, but two hosts holding the same name and the inverse **succeeds on the wrong one, silently**. An explicitly named target still wins. Line-wide: all 24 copies had the identical defect. Caught live in container-host-aiops, where a stop recorded against a Podman target replayed against a Portainer one.

## v0.5.0 — 2026-08-02

### Changed (BREAKING)
- **Requires MCP SDK 2.0** (`mcp[cli]>=2.0,<3.0`). `mcp.server.fastmcp` no longer exists in 2.0; the server is now built with `MCPServer` and reports its package version in the stdio handshake.

### Fixed
- **`undo apply` works from the CLI.** Every write tool is imported lazily inside its own CLI command, so a CLI-driven undo ran in a process where the inverse tool was never registered and failed with "inverse tool is not registered" — for every write tool. Only the MCP entry point, which imports the whole server, worked. Found while live-verifying against a real cluster.
- **An undetermined outcome is audited `unknown`, not `ok`.** The harness only classified a result as undetermined when the payload *also* carried an `error` key, so a write that looked successful but had not been confirmed was recorded as a success.
- **Every call failed against a real Xen Orchestra.** The client sent both `Authorization: Bearer` and the `authenticationToken` cookie; current XO rejects a request carrying two auth methods with `400 Having multiple authentication methods is not supported`. Only the cookie is sent now (Bearer alone is 401, both are 400). Live-verified against XCP-ng 8.3 + XO.
- **`sr rescan` 404'd**: XO names the action `scan`. Confirmed against XO's own OpenAPI spec.
- **Snapshot revert 404'd**: revert is a VM-level action, `POST /vms/{vm}/actions/revert_snapshot` with the snapshot id in the body — `/vm-snapshots/{id}/actions/revert` does not exist.
- A halted VM reported a `host` that is not a host. XO overloads `$container` as the resident host while running and the **pool** id when halted; `host` is now null when the VM is not resident on one.


## v0.4.0 — 2026-07-21

### Changed (BREAKING)
- **Removed the authorization layer** — read-only mode, the approver gate, and rules.yaml deny are gone. The skill no longer decides read vs write; that is the agent's judgement or the connecting account's permissions. `<PREFIX>_READ_ONLY` now has no effect (a startup warning is logged); `<PREFIX>_AUDIT_APPROVED_BY`/`_RATIONALE` are optional audit annotations.
- The retained guarantee is **unbypassable audit over MCP and CLI alike** — no unaudited entry point. Harness = audit + runaway safety guard + undo + sanitize; `risk_level` is a descriptive audit label, not a gate.

See RELEASE_NOTES.md for tool-specific changes.


## v0.3.0 — 2026-07-20

### Fixed
- **`vm_stop` refuses the Xen Orchestra VM when you declare it.** XO is commonly a VM on the pool it manages, and stopping it kills the request in flight; recovery needs console access.
- **CLI writes now exit non-zero on a governed error.** Eight commands previously discarded the governed result and printed success — a policy denial, an unreachable XO, or a refused self-target all reported as done..
- Harness: a write whose response is lost is audited `status=unknown`, not `error` — it may have taken effect. Undo tokens gain `effectVerified` (undo.db migrated in place).
- Harness: a dry-run no longer records an undo token, and no longer requires a named approver. Guards now run on the preview path.
- Truncated strings end in an ellipsis instead of being cut silently; error messages are capped at 800 chars, not 300.

See RELEASE_NOTES.md for the full detail.

## v0.1.0 — 2026-07-17

Initial preview release: governed XCP-ng operations via Xen Orchestra's REST
API (`/rest/v0`) with a fully bundled governance harness. **Mock-validated
only — not yet verified against a live Xen Orchestra instance.**

### Highlights

- **27 MCP tools** (19 read, 8 write), every one wrapped with the bundled
  `@governed_tool` harness (audit / policy / budget / undo / risk tiers).
- **Four flagship RCA analyses** with structured cause + action findings:
  - `vm_health_rca` — halted-unexpectedly / paused VMs, guest tools missing,
    CPU & memory pressure from RRD stats.
  - `sr_usage_rca` — SRs ranked near-full/critical, thin-provision overcommit,
    orphaned VDIs with reclaimable bytes.
  - `backup_failure_rca` — failed/skipped runs classified (vdi-chain, quiesce,
    transport, storage-full).
  - `pool_patch_ha_posture` — missing patches, pending reboots, version skew,
    HA state per pool.
- **Governed writes** with dry-run previews and honest reversibility:
  `vm_start` ↔ `vm_stop` undo pairs; `vm_migrate` records migrating back to
  the captured source host; `snapshot_create` records deleting the REAL
  snapshot id returned by XO; `snapshot_delete` / `snapshot_revert` are
  high-risk, capture prior state, and declare no undo; `sr_rescan` is a
  low-risk metadata refresh.
- **Encrypted secret store** — the XO personal authentication token is stored
  encrypted in `~/.olvm-aiops/secrets.enc` (Fernet + scrypt master password);
  never plaintext on disk. `olvm-aiops init` onboarding wizard + `secret`
  command group; `OLVM_AIOPS_MASTER_PASSWORD` for non-interactive/MCP use.
- **Token-in-header auth** against XO: both `Authorization: Bearer` and the
  `authenticationToken` cookie are sent for compatibility across XO 5.x.
- **Secure by default** — with no rules.yaml, high-risk writes are denied
  unless `OLVM_AUDIT_APPROVED_BY` names an approver; `init` seeds a starter
  rules.yaml with the dual-control tier.
- **Doctor** — config + encrypted-store checks, XO reachability, token
  validity, and managed-pool count per target.

### Scope notes

- Requires a Xen Orchestra instance (5.x, `/rest/v0`); per-host XAPI access is
  out of scope for v0.1.
- Endpoint paths are modelled against the documented XO REST API and are
  mock-validated only — verify against a live XO before trusting writes.
