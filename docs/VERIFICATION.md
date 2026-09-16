# olvm-aiops — Verification

What is guaranteed by tests, what was verified against a live engine, and what is still open.

## 1. Test suite (no engine needed)

`uv run pytest` — the platform tests load **payloads captured from a live engine**
(`tests/fixtures/`), not hand-written dicts:

| Fixture set | Engine state |
|---|---|
| `olvm-4.5.5-installing` | Fresh install: host `installing`, data center `uninitialized`, image repository `unattached`, empty VM collection (`{}`) |
| `olvm-4.5.5-host-rebooting` | Host `reboot` during the engine's 600 s post-deploy wait, jobs `started`, alert 9000 |
| `olvm-4.5.5-running` | Host and data center `up`, NFS data domain attached, a VM, disks, networks, statistics, a 404 fault, a start action traced to `up`, a 409 "disks are locked" refusal |

SSO session ids printed in login events are redacted in every fixture.

## 2. Live verification — done (2026-09-15)

Engine: **Oracle Linux Virtualization Manager 4.5.5-1.73.el9** on Oracle Linux 9.8, Keycloak
enabled (engine-setup default), one KVM host (OL 9.8, nested under another hypervisor), an NFS data domain
and one VM.

- [x] `doctor` logs in as `admin@ovirt@internalsso` and reports the product and version.
- [x] TLS: with the engine CA and an IP URL, verification fails and is reported as a TLS
      problem naming the FQDN / `ca_file` fixes, not as an unreachable engine.
- [x] SSO token: no `expires_in`; `exp` is Long.MAX as a string — renewal stays reactive on 401.
- [x] Every read: data centers, clusters, hosts, storage domains (data-center-scoped status),
      VMs, VM statistics, events (severity threshold, page, `after_index`, `since_minutes`), jobs
      (newest first) — through the real connection, no mocks.
- [x] `host_health_rca`: a host in `reboot` reported as in progress; alert 9000 as low.
- [x] `storage_capacity_rca`: the active NFS domain (86.5 % free) healthy; the unattached image
      repository skipped.
- [x] `vm_health_rca`: found a real defect in its first live run — an error event from a
      refused start kept a VM that started two minutes later flagged high. Fixed (superseded
      events are `info`) with a positive control.
- [x] Independent code review, each finding confirmed on the live engine before it was fixed:
      - event paging: with `max=limit+1`, page 2 started one row late — event 166 appeared on
        no page. The engine's page size is `max`. Fixed; a fake that pages exactly like the
        engine now walks every event once.
      - host attribution: the storage-attach event carries `host`, `storage_domain`,
        `data_center` and `cluster` refs; it is about the domain, not the host that ran it.
      - a down, non-HA VM stayed unhealthy indefinitely because of a failed start from before
        its last run. Fixed: a later start supersedes, and events have a window (24 h).
- [x] Second review (four independent reviewers: security, engine semantics, gaps, docs and
      tests). Each high finding was reproduced on the live engine before it was fixed:
      - **CLI calls were not audited.** `olvm-aiops host list` returned the host and never
        created `audit.db`; the same call over MCP wrote a row. Every CLI read now calls the MCP
        tool of the same name. Re-verified live: one audit row per command. A test fails if a CLI
        module imports the ops layer, and one checks that every read tool has an audited command.
      - **The CLI never revoked its SSO token.** A traced CLI run sent the login and the read and
        exited without a logout. Re-verified live: `POST /ovirt-engine/services/sso-logout` → 200
        at exit.
      - **The `after_index` cursor skipped events.** Cursor 153 returned 173..169; 154..168 were
        never returned. The cursor now reads oldest first (re-verified: cursor 172 → 173..176).
      - **A connect timeout was reported as a slow engine.** A black-holed address produced "the
        engine accepted the connection … raise timeout". It now says the engine is unreachable.
      - **Engine-wide alerts reached no diagnosis** (certificate expiry, no engine backup, HA
        reservation — codes from ovirt-engine's own `AuditLogType`): new `engine_health_rca`. Its
        first live run found one more false alarm: event 986 "Data Center is being initialized"
        (its `host` ref is `{"name": "Unavailable"}` with no id) stayed medium with the data
        center up. Data-center status events are now superseded while the data center is up
        (re-verified live: `healthy: true`, 986 reported as `info`).
      - Fixed from code evidence, without a live reproduction: a wrong password retried as a fresh
        login on every tool call (3 calls → 3 logins, simulated); a recovered host or auto-resumed
        VM staying high for 24 h; `eventsTruncated` true on any busy engine; a master-password
        error returned to MCP clients as "operation failed" and to the CLI as a traceback; engine
        text with rich markup breaking CLI output; `http://` URLs sending the password in clear.

Every rule added after either review has a test that fails when the rule is removed, checked by
mutating each rule and re-running its tests (second review: 43 of 43 caught).

Engine behaviour recorded along the way. Each rule the tool derives from it is covered by a test;
the start-action trace, `follow=` and the `Correlation-Id` echo are recorded for future writes
and are not exercised by a test:

| Behaviour | Consequence in the tool |
|---|---|
| Counts/sizes/flags are strings, times are epoch-ms numbers | `as_int` / `as_bool` / `ms_to_iso` everywhere |
| Attached storage domains have no status in `/storagedomains` | status joined from `/datacenters/{id}/storagedomains` |
| `/jobs` + any `search` → HTTP 400; jobs come oldest first | no search; sort newest first locally |
| Events `time > "<date>"` returns all or nothing by format; `from=<index>` is exact | `after_index` + client-side `since_minutes` |
| `follow=` on `/clusters` → HTTP 500 | not used |
| Event pages are sized by `max`; `page N` with one extra row shifts every page | page N uses `max=limit`; next page probed |
| Storage and VM events carry the host that executed them | not attributed to that host |
| Login events print the SSO session id | redacted (before truncation, so a cut cannot expose part of it) |
| `/ovirt-engine/services/health` needs no login and answers `DB Up!Welcome to Health Status!`; the API root carries `time` (epoch ms) | `engine_health_rca` (health check, clock skew) |
| `from=<index>` with `sortby time desc` returns the newest events above the cursor | the cursor reads `sortby time asc` |
| An event can carry a placeholder `host: {"name": "Unavailable"}` with no id | only refs with an id count |
| Event 986 stays in the log after the data center comes up; code 13 "Status of host … set to Up" marks a host's recovery | superseded rules |
| The host (vdsm) certificate lasts 5 years, the engine web certificate 398 days (lab: 2031-09-16 / 2027-10-18) | expiry comes from the engine's own certificate events, never a hard-coded lifetime |
| Start action: 200 `complete` in 0.23 s, VM `up` after 72.7 s; `Correlation-Id` echoed into events | no writes until each can confirm its outcome |
| Event 10802 wraps one vdsm command (`VDSM <host> command <Name>VDS failed: <message>`); a `logon` against a guest with no agent logs it with a `host` ref and **no `vm` key** | grouped per command, not per code; a guest-agent command whose message names the agent is diagnosed as a guest condition, with the host's VMs as candidates |

## 2b. Production validation by a user (2026-09-15, issue #1)

A user ran 0.1.0 against **a small production engine with 8 hosts, an FC data domain and a
self-hosted storage domain** — installation, `init`, `doctor` and the read-only MCP tools all
worked, and the results matched the engine's own API data. He reached the server over
Streamable HTTP through a wrapper of his own; this package ships **stdio only**, and that
wrapper is not part of what was verified.

Beyond the lab this covers **multi-host** (all 8 hosts evaluated) and an **FC** data domain.
It does not cover a read-only account, an engine without Keycloak, storage under pressure, or
scale past the scan limits — and everything else in §3 is still open.

He reported his run as observations, not as bugs ("the MCP behaved well in this environment").
Two of them were defects all the same, and both are fixed:

- **A guest problem reported as a host fault.** He saw event 10802
  (`VDS_BROKER_COMMAND_FAILURE`, "VDSM *host* command VmLogonVDS failed: Guest agent
  non-responsive") on two hosts that were `up`, `externalStatus: ok`, with no update or
  reinstall pending; he did not quote a severity. Reproducing the event here showed what the
  code did with it: `high`, under the generic "The engine logged a problem for this host" —
  an `error`-severity code that is in no catalogue falls through to that. The event wraps a
  single vdsm command and is logged against the host that ran it; a `VmLogon` / `VmLogoff`
  call is about a VM's guest agent. Now `low`, with the host explicitly not at fault, and
  never superseded by the host's own recovery.
- **The affected VM cannot be named from the event** — the engine sends no `vm` reference at
  all — and the user's judgement was that a single running VM on the host must stay an
  inference.
  Findings now carry `vmCandidates` (the VMs the engine reports on that host) as a candidate
  list, labelled as not confirmed in the tool description, the guardrails and the CLI.

The guest-agent defect was **reproduced and re-verified on the lab engine** (2026-09-16), not
only in tests: `POST /ovirt-engine/api/vms/{id}/logon` against a guest without a guest agent makes the
engine log exactly that event — `10802 error "VDSM olvm-kvm1 command VmLogonVDS failed: Guest
agent non-responsive"`, with a `host` ref and **no `vm` key at all**. Against the same engine
and the same event, the released 0.1.0 logic reported `1. [high] olvm-kvm1 … The engine logged
a problem for this host`; the fix reports `1. [low]`, names the guest agent, lists
`VM candidates (not confirmed): lab-vm1`, and leaves `healthy: true`. The host list is read
once and only when such a finding exists — the traced run of `host health` on an engine with
no guest-agent event sends no `/vms` request at all.

One wording change from the same run: an FC domain at **192 % committed but 43.7 % used** was
correctly `low` and `healthy: true`, but the finding did not distinguish a planning limit from
current pressure. Its signal now carries actual use, and the cause says which of the two it is.

## 3. Live checklist — still open

- [ ] An engine **without** Keycloak (`admin@internal`).
- [ ] A **read-only** account (`ReadOnlyAdmin`): every read and diagnosis works; data-center
      storage views are readable (otherwise `statusErrors` must name the data center).
- [ ] **Multi-host** cluster — partly done: 8 hosts evaluated on a production engine (§2b).
      Open: SPM on one host and one host `non_responsive` (stop vdsmd) — the finding must
      carry `status_detail`.
- [ ] **Storage faults**: fill a domain below `warning_low_space_indicator` and below
      `critical_space_action_blocker` (the engine must refuse a new disk — confirm the critical
      finding matches that refusal); put a domain in maintenance; block the NFS server so the
      domain goes `inactive`.
- [ ] **Paused VM**: exhaust a thin domain so a guest pauses on I/O error; `vm_health_rca` must
      rank it high and point at storage.
- [ ] **Scale**: more than 1000 VMs or events — the truncation and scan flags must turn true.
- [ ] Storage back-ends and deployments — partly done: an **FC** data domain read correctly,
      with a self-hosted storage domain in the same engine (§2b). Open: iSCSI and Gluster
      domains, a self-hosted **engine** deployment as such, and OLVM on Oracle Linux 8.
- [ ] An engine behind a directory profile (LDAP / AD user@profile).
- [ ] An engine that has run for days: host and VM events on both sides of the 24 h window
      (`eventsOutsideWindow`), and a VM that failed, started and was shut down again.
- [ ] Login backoff: change the account password while the MCP server runs — one failed
      login per 60 s, not one per call (only simulated so far).
- [ ] Certificate and backup alerts on an engine that raises them (845–849, 876–883, 9022,
      9023): `engine_health_rca` / `host_health_rca` must report them with their own cause.
- [ ] A recovered host (stop and start vdsmd) and an auto-resumed paused VM: their old events
      must be superseded.

## 4. Writes (not in this release)

Before any write tool ships, verify on a live engine for that action: the action response, the
object's state transitions until its target state, the job's final status, and that a
`Correlation-Id` request header ties the resulting events to the call — and that the audit row
and any undo token reflect the confirmed outcome, not the engine's immediate `complete`.
