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
