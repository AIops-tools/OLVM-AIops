# Changelog

## Unreleased

First production feedback ([#1](https://github.com/AIops-tools/OLVM-AIops/issues/1)): a run
against a small production engine with 8 hosts and an FC data domain.

### Added
- `host_health_rca` findings for a failed guest-agent call carry `vmCandidates`: the VMs the
  engine reports on that host, as `{vms, returned, limit, truncated, total, scanTruncated,
  error}`. The event names no VM, so these are candidates to check — the tool description, the
  agent guardrails and the CLI ("VM candidates (not confirmed)") all say so. This adds one
  `/vms` read to the diagnosis, made once and only when such a finding exists; a VM list that
  cannot be read (a restricted account) is reported as `error` with `total: null`, never as a
  host running nothing, and does not fail the diagnosis. `scanTruncated` says the VM scan
  itself was cut at 1000, which makes `total` a lower bound. When the read failed there is no
  scan to describe, so `truncated` and `scanTruncated` are `null` rather than `false`. The
  envelope's `limit` is fixed; for the whole list on a busy host, read `vm_list` with
  `search="host=<name>"`.
- The over-commit finding's signal carries actual use next to the commitment
  (`committed 192.0% of capacity, in use 43.7% (56.3% free)`).

### Fixed
- A failed guest-agent call is no longer reported as a host fault. Event 10802
  (`VDS_BROKER_COMMAND_FAILURE`) wraps one vdsm command, and the engine logs it against the
  host that ran the call: `VmLogonVDS failed: Guest agent non-responsive` was ranked `high`
  under the generic "The engine logged a problem for this host" while the host was `up`,
  `externalStatus: ok`, with nothing pending. A `VmLogon` / `VmLogoff` command whose message
  names the guest agent is now `low`, says the host itself is not at fault, and is not
  superseded by the host's own recovery. The same command failing for another reason (a vdsm
  transport timeout, say) is about the host's link to vdsm and stays a host finding, as does
  every other vdsm command failure.
- Event 10802 is now grouped per condition — the command, plus whether the message names the
  guest agent — instead of per code. It wraps every vdsm command, so one group per host and
  code let the newest member classify the rest: a `SpmStatusVDS` failure followed by a
  guest-agent one was reported as the guest-agent finding, and its text appeared nowhere in
  the payload. The command alone is not enough either, because one command reports both
  conditions: a `VmLogonVDS` transport timeout must not be collapsed into a newer
  `VmLogonVDS` guest-agent failure.
- The over-commit cause no longer claims free space is "within the domain's own thresholds"
  when the engine set none. It reports 0 for a domain with no low-space warning, and 0 can
  never be crossed; the finding now says no check was made and points at the free space in
  its own signal.
- The over-commit finding now says which of two things it is: a planning limit while free
  space holds, or — together with a low-space finding — a promise that cannot be kept.
- The low-space action quoted a threshold the engine had not reported as `None GiB`.

## v0.1.0 — 2026-09-15

First release of olvm-aiops: governed operations for Oracle Linux Virtualization
Manager (OLVM) and upstream oVirt 4.5 engines, built from the AIops-tools line
template (vendored governance harness, encrypted secret store, CLI + MCP server).

### Added
- Engine connection over the REST API without the official SDK (which needs
  `pycurl` built from source): SSO password-grant login with the mandatory
  `ovirt-app-api` scope, `Version: 4` requests, the engine's own fault reason and
  detail in every error, one re-login and retry on 401 (the engine issues no
  refresh token), renewal serialised across threads, and timeouts reported as
  timeouts rather than connectivity faults.
- Targets carry the username with its profile (`admin@internal`, or
  `admin@ovirt@internalsso` on engines where setup enabled Keycloak — the default
  since 4.5.1), optional `ca_file` for the engine CA, and a per-target `timeout`.
- `olvm-aiops init` wizard and `olvm-aiops doctor` (login + engine product
  version); passwords stored encrypted.
- A 2xx response that is not JSON (a proxy error or SSO login page) is an error,
  not an empty collection, and so is an empty GET body; after a refused login —
  first or renewal — the target fails fast for 60 s so a changed password cannot
  lock the account; concurrent first calls share one engine session.
- Only `https://` engine URLs are accepted (login sends the password in the
  body); a connect timeout is reported as an unreachable engine, not a slow one;
  a wrong master password reaches MCP clients and the CLI as its own message.
- Every CLI read calls the MCP tool of the same name, so it is audited, budgeted
  and exits non-zero on failure; the CLI revokes its SSO token when it exits.
- Reads: data centers, clusters, hosts, storage domains (status joined from each
  data center), VMs and statistics, events (severity threshold, `page`,
  `after_index` cursor returning the events after it oldest first,
  `since_minutes`) and jobs (newest first), each listing with measured truncation.
- Diagnoses `engine_health_rca`, `host_health_rca`, `storage_capacity_rca` and
  `vm_health_rca`: findings ranked worst first with signal, cause and action.
  Every warning-or-worse event in the last `events_window_hours` (default 24)
  belongs to exactly one of them, one finding per subject and code; certificate,
  engine-backup, HA-reservation, storage-deactivation and time-drift events carry
  their own cause; a host set to Up, a VM started or reported back up, and a data
  center that is up again supersede their earlier events; `eventsTruncated` means
  the window itself was cut; over-commit alone is low.
