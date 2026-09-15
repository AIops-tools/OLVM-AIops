# Changelog

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
