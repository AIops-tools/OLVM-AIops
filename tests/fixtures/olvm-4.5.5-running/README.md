# OLVM 4.5.5-1.73.el9 — running engine

Captured by `api_probe.py` on a live engine (Keycloak on) after bring-up: one KVM host `up`,
data center `Default` `up`, an NFS data domain attached and `active`, one VM (`lab-vm1`,
down), its disk, the management network and the Blank template. Session ids printed in
login events are replaced with `REDACTED-SESSION-ID`; the API root's `link` list is dropped.

Notable, verified shapes:
- `storagedomains.json`: the attached NFS domain has **no top-level `status`** — status of
  an attached domain lives under `/datacenters/{id}/storagedomains`. Capacity is strings:
  `available`, `used`, `committed` bytes; `warning_low_space_indicator` (%) and
  `critical_space_action_blocker` (GB).
- `host_statistics.json`: gauges like `{"name": "memory.total", "unit": "bytes",
  "values": {"value": [{"datum": 9946791936}]}}` — `datum` is a JSON number.
- `fault_404.json`: status and body of a GET on a non-existent VM id.
- `vm_start_409_disk_locked.json`: the action response when starting a VM seconds after its
  disk-add call returned 201 — the disk was still being created. A 201 is not "done".
- `vm_start_200_accepted.json` + `vm_start_status_trace.json`: starting `lab-vm1` answered
  **HTTP 200 in 0.23 s with `"status": "complete"`** and a job link, while the VM went
  `wait_for_launch` → `powering_up` (+2 s) → `up` (+72.7 s). "complete" means the engine
  accepted the command, not that the VM runs. A `Correlation-Id` request header is echoed
  into the resulting events (codes 153 and 32).
- `vm_statistics.json`, `vm_after.json`: the VM once `up`.
- `follow.json`: `GET /clusters?follow=hosts` answered **HTTP 500** on this engine.
- `jobs.json`: jobs arrive **oldest first**.
- `datacenter_storagedomains.json` / `datacenter_storagedomain_get.json`: the data-center
  scoped view — the attached NFS domain is `active`, `master: "true"`, committed 1 GiB.
- `storagedomains_global.json` / `storagedomain_get.json`: the same domain globally, with
  **no status field at all**; the unattached Glance domain keeps `status: "unattached"`.
- `storagedomain_disks.json`: the domain's disks (`lab-vm1-disk`, `ok`).
