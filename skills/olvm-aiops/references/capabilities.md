# olvm-aiops — Capabilities (16 MCP tools: 14 read, 2 undo)

Every tool is `[READ]`, `risk_level=low`, wrapped by `@governed_tool` (audited) and
`@tool_errors` (failures come back as `{"error", "hint"}`). Engine: OLVM 4.5 / oVirt 4.5,
REST API `/ovirt-engine/api`, JSON with `Version: 4`.

Conventions in every payload:
- A field the engine did not report is `null` — never an invented `0`, `false` or `""`.
- Counts and sizes are real integers (the engine sends them as strings; they are converted).
- Times are ISO-8601 UTC (the engine sends epoch milliseconds).
- A listing with `limit` returns `returned`, `limit` and `truncated`; `truncated` is measured
  by asking the engine for one extra row.
- Findings carry `rank` (1 = worst), `severity`, `signal`, `cause`, `action`.
- A 2xx answer that is not JSON (a proxy error or SSO login page) is an error, never an empty
  collection.

## Diagnosis

| Tool | Reads | Findings |
|---|---|---|
| `host_health_rca` | `/hosts`, `/events?search=severity>normal sortby time desc` | high: `non_operational`, `non_responsive`, `install_failed`, `error`, `down`, `kdumping` (with `status_detail`); info: `installing`, `reboot`, `connecting`, `initializing`, `preparing_for_maintenance`, `maintenance`; medium: `reinstallation_required`; low: `update_available`; external status `error`/`failure` = high, `warning` = medium; host events from the last `events_window_hours` (default 24), one finding per host and event code with the repeat count (alert 9000 power-management = low). An event that also names a VM or storage domain is about that object and is not attributed to the host that ran it; older events are counted in `eventsOutsideWindow`. |
| `storage_capacity_rca` | storage domains + each data center's storage domains | critical: free space below `critical_space_action_blocker` GiB (engine refuses new disks and snapshots); medium: free % below `warning_low_space_indicator`, external status warning; committed > 100 % of capacity — low on its own (thin provisioning), medium together with low space; high: attached domain `inactive` / `unknown` / `mixed`, external status error; info: maintenance or transitions; medium: status unreadable. Unattached domains are skipped. |
| `vm_health_rca` | `/vms`, warning+ events | high: `not_responding`, `unknown`, `paused` (storage first), HA VM `down`; medium: `image_locked`; info: transitions (`migrating`, `powering_up`, …); low: config change pending restart; VM events from the last `events_window_hours` (default 24) — superseded (info) when older than the VM's latest start, whatever its state now |

## Inventory

| Tool | Reads | Row highlights |
|---|---|---|
| `datacenter_list` | `/datacenters` | `status`, `compatibilityVersion`, `local`, `quotaMode` |
| `cluster_list` | `/clusters` | `compatibilityVersion`, `cpuType` (null until a host joins), `memoryOverCommitPct`, `ballooningEnabled`, `upgradeInProgress` |
| `host_list` / `host_get` | `/hosts` (`search` passed through) | `status`, `statusDetail`, `spmStatus`, `memoryBytes`, `maxSchedulingMemoryBytes`, `vmsActive`/`vmsTotal`, CPU topology, `osVersion`, `updateAvailable`, `reinstallationRequired` |
| `storage_domain_list` / `storage_domain_get` | `/storagedomains` + `/datacenters/{id}/storagedomains` | `status` + `statusSource` (`dataCenter` / `global` / null), `master`, `availableBytes`, `usedBytes`, `committedBytes`, `totalBytes`, `usedPct`, `freePct`, `committedPctOfTotal`, thresholds; `statusErrors` |
| `vm_list` / `vm_get` | `/vms` (`search` passed through) | `status`, `hostId`, `vcpus`, `memoryBytes`, `highAvailability`, `restartPendingForConfig`, `startTime`, `stopTime` |
| `vm_stats` | `/vms/{id}/statistics` | `{name: {value, unit}}` — memory bytes, CPU %, network, `disks.usage`, `elapsed.time` |

Why storage status is joined: an attached domain has no `status` in `/storagedomains`
on a live engine; reading only that collection reports every attached domain as unknown.

## Activity

| Tool | Parameters | Notes |
|---|---|---|
| `event_list` | `limit`, `min_severity` (normal/warning/error/alert), `page`, `after_index`, `since_minutes` | Newest first. `page` N is read with the engine's page size (`max=limit`) and `truncated` by probing page N+1 — one extra row there shifts every page and hides a row at each boundary (seen live). `after_index` uses the engine's `from=` cursor. `since_minutes` filters on each event's own time over a bounded scan; `scanTruncated` is true only when the scan was cut while its oldest event was still inside the window; the engine's `time` search is not used — on a live engine it returned all or nothing depending on date format. SSO session ids in login events are redacted. |
| `job_list` | `limit`, `status` (started/finished/failed/aborted/unknown) | The engine refuses `search` on `/jobs` (HTTP 400) and returns jobs oldest first; jobs are read in bulk, sorted newest first and filtered here (`scanTruncated`). |

## Governance

| Tool | Notes |
|---|---|
| `undo_list` / `undo_apply` | The harness undo log. This release has no writes, so nothing records an undo. |

## Not in this release

Writes of any kind. On a live engine, `POST /vms/{id}/start` answered HTTP 200 with
`status: complete` in 0.23 s while the VM reached `up` 72.7 s later, and a start issued right
after a disk add (201) was refused with 409 "disks are locked". A write tool must poll the
object (or correlate events through a `Correlation-Id` header, which the engine honours)
before reporting success.
