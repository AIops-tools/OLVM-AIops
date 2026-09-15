# OLVM 4.5.5-1.73.el9 — host rebooting after deploy

Captured from a live engine (Keycloak enabled) in the window after host deploy, when the
engine's `SshHostReboot` command had rebooted the new KVM host and was waiting its fixed
600 seconds before reconnecting. Session ids in login events are replaced with
`REDACTED-SESSION-ID`; nothing else is edited.

- `hosts.json`: `olvm-kvm1` in status `reboot` (the host itself was already back up).
- `jobs.json`: "Adding new Host …" and "SshHostReboot", both `started`.
- `events_warning_plus.json`: `severity>normal`, including alert 9000 "Failed to verify
  Power Management configuration" — expected on hosts without fencing hardware.
- `datacenters.json`: `Default` still `uninitialized`.

A host health diagnosis must not call this state a failure: `reboot` during an engine-driven
reboot window is transient, and the PM alert is informational in a lab without IPMI.
