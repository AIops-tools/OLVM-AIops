# olvm-aiops — CLI reference

Global pattern: `olvm-aiops <group> <command> [args] [--target <t>]`.
`--target/-t` selects a Xen Orchestra target from `~/.olvm-aiops/config.yaml`
(default: the first one). Write commands support `--dry-run` (prints the API
call, changes nothing) and destructive ones require **double confirmation**.

## Setup & health

```bash
olvm-aiops init                    # onboarding wizard: XO URL, TLS verify (default yes), encrypted token
olvm-aiops doctor [--skip-auth]    # config + secret store + XO reachability + pool count
olvm-aiops overview [-t xo1]       # one-shot fleet health summary (JSON)
olvm-aiops mcp                     # start the MCP server (stdio)
```

## VMs

```bash
olvm-aiops vm list [--state Running|Halted|Paused|Suspended] [--pool <pool_uuid>] [--limit 200]
olvm-aiops vm get <vm_uuid>
olvm-aiops vm stats <vm_uuid> [--granularity seconds|minutes|hours|days]
olvm-aiops vm health-rca [<vm_uuid>]          # RCA (fleet-wide when uuid omitted)
olvm-aiops vm start <vm_uuid> [--dry-run]                 # governed; undo = stop
olvm-aiops vm stop <vm_uuid> [--force] [--dry-run]        # double confirm; undo = start
olvm-aiops vm reboot <vm_uuid> [--force] [--dry-run]      # double confirm; no undo
olvm-aiops vm migrate <vm_uuid> <host_uuid> [--dry-run]   # double confirm; undo = migrate back
```

`--force` = hard power operation (no guest tools needed); default is a clean
guest shutdown/reboot.

## Hosts

```bash
olvm-aiops host list [--pool <pool_uuid>]
olvm-aiops host get <host_uuid>
olvm-aiops host missing-patches <host_uuid>
```

## Pools

```bash
olvm-aiops pool list
olvm-aiops pool get <pool_uuid>
olvm-aiops pool posture [<pool_uuid>]   # RCA: patches / reboots / version skew / HA
```

## Storage (SRs / VDIs)

```bash
olvm-aiops sr list [--pool <pool_uuid>] [--limit 200]
olvm-aiops sr get <sr_uuid>
olvm-aiops sr vdis [--sr <sr_uuid>] [--orphaned-only] [--limit 200]
olvm-aiops sr usage-rca                 # RCA: near-full / overcommit / orphaned VDIs
olvm-aiops sr rescan <sr_uuid> [--dry-run]
```

## Snapshots

```bash
olvm-aiops snapshot list [--vm <vm_uuid>] [--limit 200]
olvm-aiops snapshot create <vm_uuid> <name> [--dry-run]
olvm-aiops snapshot delete <snapshot_uuid> [--dry-run]    # double confirm, IRREVERSIBLE
olvm-aiops snapshot revert <snapshot_uuid> [--dry-run]    # double confirm, IRREVERSIBLE
```

## Backups & tasks

```bash
olvm-aiops backup jobs [--limit 200]
olvm-aiops backup logs [--limit 50]
olvm-aiops backup failure-rca [--limit 50]   # RCA: vdi-chain / quiesce / transport / storage-full
olvm-aiops task list [--status pending|success|failure] [--limit 200]
```

## Secrets (encrypted store)

```bash
olvm-aiops secret set <target> [--value <token>]   # omit --value to be prompted (hidden)
olvm-aiops secret list                             # names only
olvm-aiops secret rm <target>
olvm-aiops secret migrate                          # import legacy plaintext .env
olvm-aiops secret rotate-password                  # re-encrypt under a new master password
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OLVM_AIOPS_MASTER_PASSWORD` | Unlock `secrets.enc` non-interactively (MCP/CI). |
| `OLVM_AIOPS_HOME` | Relocate `~/.olvm-aiops` (audit.db, undo.db). |
| `OLVM_AIOPS_CONFIG` | Alternate config.yaml path for the MCP server. |
| `OLVM_AUDIT_APPROVED_BY` / `OLVM_AUDIT_RATIONALE` | Optional approver/rationale annotations recorded on the audit row (never required). |
| `OLVM_MAX_TOOL_CALLS` / `OLVM_MAX_TOOL_SECONDS` | Budget ceilings. |
| `OLVM_RUNAWAY_MAX` / `OLVM_RUNAWAY_WINDOW_SEC` | Runaway-loop circuit breaker. |
| `OLVM_<TARGET>_TOKEN` | Legacy plaintext token fallback (deprecated). |

## Truncation

Listing commands cap their output at `--limit` (default 200) and print
`… showing N of more … — truncated, re-run with a higher --limit` when there
was more; the JSON itself carries `"truncated": true`.
