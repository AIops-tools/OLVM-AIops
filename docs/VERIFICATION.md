# Live verification — olvm-aiops

## ✅ Live-verified against real XCP-ng 8.3 + Xen Orchestra (2026-08-01)

Verified end-to-end against a **real XCP-ng 8.3.0 host** (nested-KVM lab)
managed by a real Xen Orchestra (`/rest/v0`, personal token) — reads
cross-checked against `xe` on the host, and the full write → audit → undo loop
closed on real objects. **Four real bugs, all fixed and regression-tested;
three of them meant the affected call could never have worked against a real
XO.**

### The action-path audit (the highest-suspicion code)

Every REST path the tool constructs was probed against the live XO **and**
reconciled with XO's own OpenAPI spec (`/rest/v0/docs/swagger.json`, 223 paths):

| Tool path | Live result | Verdict |
|---|---|---|
| `POST /vms/{id}/actions/snapshot` | 201 | ✅ correct |
| `POST /vms/{id}/actions/start` / `hard_shutdown` | 204 | ✅ correct |
| `POST /vms/{id}/actions/clean_shutdown` / `clean_reboot` / `hard_reboot` | 500 on VM state | ✅ route exists |
| `POST /vms/{id}/actions/migrate` | 422 schema | ✅ route exists |
| `DELETE /vm-snapshots/{id}` | 200 | ✅ correct |
| `GET /hosts/{id}/missing_patches` | 200 | ✅ correct |
| **`POST /srs/{id}/actions/rescan`** | **404** | 🔴 **does not exist** → `scan` |
| **`POST /vm-snapshots/{id}/actions/revert`** | **404** | 🔴 **does not exist** → VM-level `revert_snapshot` |

### Bugs found and fixed

1. **🔴 Two auth methods sent at once broke EVERY call.** The client sent both
   `Authorization: Bearer <token>` and `Cookie: authenticationToken=<token>`
   ("so one client works across XO versions"). Current XO rejects that with
   `400 "Having multiple authentication methods is not supported, please choose
   one"`. Verified live: cookie alone → 200, Bearer alone → 401, both → 400.
   Fixed to send the **cookie only**.
2. **🔴 `sr rescan` could never succeed** — XO names the action **`scan`**;
   `rescan` 404s and is absent from the spec. Fixed; live-verified (204).
3. **🔴 `snapshot revert` could never succeed** — revert is a **VM-level**
   action, `POST /vms/{vm}/actions/revert_snapshot` with `{"snapshotId": ...}`
   in the body. The tool posted to `/vm-snapshots/{id}/actions/revert` (404).
   Fixed (resolving the parent VM from the snapshot's `$snapshot_of`, and
   failing loudly rather than guessing when it is absent); live-verified (204).
4. **A halted VM reported a `host` that is not a host.** XO overloads
   `$container`: the resident host while running, the **pool id** when halted.
   The tool mapped it straight to `host`, so a halted VM advertised a uuid that
   resolves to no host. Now `null` when the VM is not resident on a host.

### Live loop that passed after the fixes

`doctor` (1 pool managed) · `overview` (1 pool / 1 host 8.3.0 / 1 VM Halted /
4 SRs — matching `xe`) · `vm|host|sr|pool list` · `snapshot create` → real
snapshot confirmed by `xe snapshot-list` → `undo apply` → **snapshot gone from
the host** (`effectVerified: true`) · `sr rescan` (204) · `snapshot revert`
(204) · `host missing-patches` (real patch list). Every governed write landed an
audit row, including an honest `sr_rescan | error` for the pre-fix 404.

> **Lab recipe:** XCP-ng 8.3 installs unattended under nested KVM via a remastered
> ISO (answerfile on the dom0 kernel cmdline). Three traps: the installer needs a
> **≥46 GB** primary disk; you must **preserve the ISO volume label** (the
> installer locates its package repo by label, so `-V` renaming makes it fail with
> an empty disk); and drop `console=tty0` so the installer's TUI/errors reach the
> serial log. XO must run with `--network host` — a Docker-bridge container cannot
> reach libvirt's NAT network.

---

## Earlier partial run — connection/auth/reads only (superseded by the above)

Verified the **connection + auth + read layer** against a real Xen Orchestra
(`/rest/v0`, `ronivay/xen-orchestra`, personal token):

- **🔴 The client sent TWO auth methods at once** — both
  `Authorization: Bearer <token>` and `Cookie: authenticationToken=<token>`
  ("so one client works across XO versions"). Current XO **rejects that** with
  `400 "Having multiple authentication methods is not supported, please choose
  one"`, so doctor and every read failed. Verified against the live REST:
  `Cookie` alone → 200, `Bearer` alone → 401, both → 400. Fixed to send the
  **cookie only** (portable across all XO 5.x); regression-tested.
- After the fix: `doctor` connects (token auth over the real `/rest/v0`);
  `overview`, `vm list`, `sr list`, `host/pool list`, `backup jobs`, `task list`
  all return correctly and handle an **empty** XO gracefully — proper truncation
  envelopes (`{items, returned, limit, truncated}`), no crash-on-empty.

**This gap is now CLOSED** — see the full run above, which stood up a nested
XCP-ng 8.3 host, connected it in XO, and exercised every action path against real
objects.

---

Remaining gaps: **backup jobs/logs against real backup runs** (no backup
repository was configured), **multi-host pool** behaviour (HA, migration between
hosts), and **RCA on a loaded cluster** (the lab host was idle, so the vm-health /
sr-usage analyses had no real fault to find).

The action names have now been reconciled against a live XO and its OpenAPI spec
(see the table above) — **two of them were wrong**, which is exactly the drift
this section used to warn about. Re-run that reconciliation against any new XO
major release before assuming the paths still hold.

The checklist below defines what a live verification run must cover. It is
deliberately checklist-shaped so the result is reproducible and auditable — not a
subjective "seems fine".

## What the mock suite already guarantees

- Every module imports; the CLI builds; every MCP tool carries the
  `@governed_tool` harness marker (`tests/test_smoke.py`).
- The four RCA analyses (`vm_health_rca`, `sr_usage_rca`,
  `backup_failure_rca`, `pool_patch_ha_posture`) are unit-tested against
  synthetic XO payloads, including their thresholds, classifications and
  rankings.
- Write tools carry the correct risk tier and record the correct inverse undo
  descriptor against a mocked connection: `vm_start` ↔ `vm_stop`;
  `vm_migrate` records migrating back to the **captured** source host;
  `snapshot_create` records deleting the **real** snapshot id XO returned.
- `snapshot_delete` / `snapshot_revert` are `high` risk, capture BEFORE state,
  and honestly record **no** undo.
- `dry_run=True` on every write returns a preview, records no undo, and makes no
  write call. It MAY read — that is how it evaluates guards and resolves ids —
  and it is audited like any other governed call, on the CLI as well as MCP.
- `vm_stop` refuses the target's declared `xo_self_vm_uuid` on both the MCP and
  CLI paths and on `dry_run` too, refuses no other uuid, and refuses nothing
  when undeclared. The IP hint never blocks on either path.
- `vm_migrate` stashes the pre-move source host via `capture_prior_state`
  BEFORE the POST, so the inverse is recorded even when the response is lost.

What it does **not** guarantee: that those REST paths, XO action names, RRD
stat shapes and backup-log field names exist as modelled on any real XO build —
the mock suite passed 100% while two action paths 404'd on every real XO. The
VM/SR/host/snapshot paths are now live-confirmed (see the table above); **RRD
stat shapes and backup-log fields remain unconfirmed**.

It also does **not** guarantee that stopping the Xen Orchestra VM is prevented.
The guard is exact but opt-in: with no `xo_self_vm_uuid` declared on the target,
nothing is refused. XO's REST API has no self endpoint and its static token
carries no claims, so this cannot be discovered today, and the tool fails open
rather than guess. The IP-based `selfVmHint` in the dry-run is advisory only —
it is silent without the guest agent and fires on every VM behind a shared
proxy or NAT. A live run should confirm both: that the declared uuid is refused
and that an undeclared target is not.

## Prerequisites for a live run

Live verification is **not** as cheap as a container here — you need a real
XCP-ng host (or nested XCP-ng) plus a Xen Orchestra instance:

- **Xen Orchestra 5.x** with the REST API `/rest/v0` enabled — XO from sources
  or the Xen Orchestra Appliance. XO is the management plane; per-host XAPI is
  out of scope.
- At least one XCP-ng host in a pool. A **two-host pool** is needed to cover
  migration and version-skew findings; a single host still covers most boxes.
- An XO **authentication token** with least privilege, and a **throwaway test
  VM** you are willing to stop, reboot, snapshot, revert, and migrate.

Never verify against production VMs.

```bash
uv tool install olvm-aiops
olvm-aiops init      # XO URL + token, stored encrypted
```

Record the XO version and the XCP-ng version — this tool's main risk is
version drift, so a result without versions is not a usable result.

## Verification checklist

Tick every box. A box that cannot be ticked is a verification gap — record it,
do not silently pass.

### 1. Connectivity (the fastest live gate)
- [ ] `olvm-aiops doctor` → all green: config, encrypted secret store, XO
      reachable over `/rest/v0`, and a real pool count returned.

### 2. Reads return real, well-shaped data
- [ ] `olvm-aiops overview` → pools, hosts, VMs by state and SRs match the XO
      UI; no crash on fields this XO release omits.
- [ ] `olvm-aiops vm list` / `vm get <uuid>` → real VMs with populated uuid,
      name, power state, host and guest-tools status.
- [ ] `olvm-aiops vm stats <uuid>` → RRD series parse into usable numbers
      (the RRD payload shape is a likely drift point).
- [ ] `olvm-aiops host list` / `host get <uuid>` / `host missing-patches
      <uuid>` → the patch list matches what XO shows for the host.
- [ ] `olvm-aiops pool list` / `pool get <uuid>` → real pools.
- [ ] `olvm-aiops sr list` / `sr get <uuid>` / `sr vdis --sr <uuid>` → SR
      sizes match the XO UI; `--orphaned-only` returns genuinely unattached
      VDIs (no false positives).
- [ ] `olvm-aiops snapshot list` → existing snapshots are listed with real ids.
- [ ] `olvm-aiops backup jobs` / `backup logs -n 20` → real XO backup jobs and
      runs (the `/backup/logs` shape is a likely drift point).
- [ ] `olvm-aiops task list` → in-flight XO tasks appear.

### 3. RCAs judge correctly against reality
- [ ] `olvm-aiops vm health-rca` → a deliberately halted or paused test VM is
      flagged with the right cause; a healthy VM is not (no false positive).
- [ ] `olvm-aiops sr usage-rca` → the near-full (≥85%) and critical (≥95%)
      thresholds fire against measured SR fullness; overcommit and orphaned-VDI
      reclaimable bytes are plausible.
- [ ] `olvm-aiops backup failure-rca` → a genuinely failed backup run is
      classified into the correct bucket (vdi-chain / quiesce / transport /
      storage-full), not "unknown".
- [ ] `olvm-aiops pool posture` → missing patches, pending reboots and version
      skew match the XO UI for the pool.

### 4. A reversible write + its undo (governance closes the loop)
- [ ] `olvm-aiops vm stop <test-vm> --dry-run` → prints the API call, changes
      nothing.
- [ ] `olvm-aiops vm stop <test-vm>` → the VM actually stops; the result
      carries an `_undo_id`; a row lands in `~/.olvm-aiops/audit.db`.
- [ ] `olvm-aiops undo apply <id>` → the recorded inverse (`vm_start`) runs and
      the VM comes back up.
- [ ] `olvm-aiops snapshot create <test-vm> verify-snap` → the recorded undo
      names the **real** snapshot id XO returned (not a guessed one); `undo
      apply` deletes exactly that snapshot.
- [ ] Two-host pool only: `olvm-aiops vm migrate <test-vm> <other-host>` then
      `undo apply` → the VM returns to its **captured** original host (proves
      the source host was captured, not guessed).
- [ ] `olvm-aiops sr rescan <sr-uuid>` → completes and is audited as a low-risk
      write.

### 5. Governance records, it does not gate
- [ ] A `high`-risk op (`snapshot delete` or `snapshot revert`) runs with no
      approver set — the skill does not gate it — and lands an audit row with a
      descriptive `risk_tier` (`review`) and **no** undo token (it is
      irreversible and says so).
- [ ] Setting `OLVM_AUDIT_APPROVED_BY` / `OLVM_AUDIT_RATIONALE` annotates the
      audit row with who/why; it neither blocks the call nor is required.
- [ ] Every CLI write lands the same audit row the MCP path does — there is no
      unaudited entry point.
- [ ] A tight poll loop trips the runaway budget guard rather than hammering XO.
- [ ] A failed operation is audited with `status=error` and records no undo.

### 6. Async XO tasks are polled, not re-issued
- [ ] A long action (migrate, or a snapshot of a large VM) is tracked via
      `olvm-aiops task list` to completion without the action being re-issued.

### 7. Cleanup
- [ ] Delete the test snapshots; confirm each delete is audited and tagged
      `high`.
- [ ] Return the test VM to its original host and power state.
- [ ] Remove the throwaway XO token from the secret store
      (`olvm-aiops secret rm <name>`) and revoke it in XO.

## Criteria to consider it live-verified

Record this tool as live-verified **only when all of the following hold**:

1. Every checklist box in sections 1–5 and 7 is ticked against at least one
   real XO instance, with the **XO version and XCP-ng version recorded**
   (e.g. "verified on XO 5.x / XCP-ng 8.3").
2. Sections 4 (migration) and 6 are either ticked, or recorded as explicit
   named gaps if only a single-host pool was available — never quietly skipped.
3. Every REST path or action-name mismatch found during the run is fixed and
   covered by a regression test, and the XO version where it differs is noted.
4. The run is written up in this repo's release notes with the date and
   version, matching how the line records its other live-verified tools.

Until then this document stands as the accurate statement of status.

## Notes for maintainers

- `olvm-aiops doctor` is the single fastest live entry point; start there.
- Expect the first failures at the **action endpoints**, not the read
  endpoints — XO renames actions between releases more often than it changes
  object shapes.
- The verification story for the whole product line is tracked centrally; add
  this tool's result there once green so the verification-debt ledger stays
  accurate.
