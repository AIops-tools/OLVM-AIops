# OLVM 4.5.5-1.73.el9 — captured mid-install

Raw JSON bodies (`Accept: application/json`, `Version: 4`) from a live Oracle Linux
Virtualization Manager engine with Keycloak enabled, captured while the first KVM host
was still being added. Unedited apart from dropping the API root's `link` list and replacing the SSO
session id the engine prints in its login event (code 30) with `REDACTED-SESSION-ID`.

What this snapshot contains, and why it is useful for tests:
- `hosts.json`: one host in status `installing`, `memory "0"`, `status_detail` null.
- `storagedomains.json`: only the default Glance `ovirt-image-repository`, `unattached`
  (type `image`) — present on every fresh install and not a fault.
- `datacenters.json`: `Default` in status `uninitialized` (no storage attached yet).
- `jobs.json`: "Adding new Host olvm-kvm1 to Cluster Default", status `started`.
- `events.json`: 20 events, severity `normal`, `time` as epoch-ms JSON numbers.
- `vms.json`: `{}` — an empty collection omits its key.

Conventions visible here: counts/sizes/flags are strings, timestamps are numbers.
