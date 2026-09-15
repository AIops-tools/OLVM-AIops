# Changelog

## Unreleased — v0.1.0

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
  not an empty collection; after a failed re-login, renewals fail fast for 60 s
  so a changed password cannot lock the account; concurrent first calls share
  one engine session.
- Reads: data centers, clusters, hosts, storage domains (status joined from each
  data center), VMs and statistics, events (severity threshold, `page`,
  `after_index` cursor, `since_minutes`) and jobs (newest first), each listing
  with measured truncation.
- Diagnoses `host_health_rca`, `storage_capacity_rca`, `vm_health_rca`: findings
  ranked worst first with signal, cause and action. Host and VM events count only
  from the last `events_window_hours` (default 24); host events are one finding
  per code with a repeat count and are never taken from events about a VM or a
  storage domain; any later VM start supersedes an earlier error event;
  over-commit alone is low.
