# Implementation progress

This file tracks executable evidence against DESIGN.md. Initial development uses
logical commits on main; PRs remain paused. The full design and real research
pilot are not complete.

## Verified evidence

- 342 Python tests pass natively and in isolated Linux CI.
- Ruff lint and format checks pass.
- Forty-seven desktop connection/IPC and renderer tests pass using Node 22.12.
- Explicit migrations through schema v11 preserve historical records, input bytes,
  active grants, leases and held exposure; injected migration faults roll back.
- No tests were skipped. Dependency deprecation warnings remain.

These include component tests and synthetic live acceptance of container
isolation, read-only released inputs, control revocation, duration stops, and
sandboxed Electron startup. Visual QA, scientific output validation, and a
representative investigation have not passed end-to-end acceptance.

## Reviewed implementation steps

1. **Workspace adoption:** bounded read-only inventory, reviewed-digest byte
   capture, immutable retained snapshots and integrity verification. Unsafe paths,
   links and malformed manifests fail closed. See [workspace adoption](workspace-adoption.md).
2. **Control records:** immutable plan history and approval digests; scoped,
   expiring credentials with parent revocation; transactional state/event/replay
   writes. Conversations and human decisions remain durable intent records.
3. **Research ledger:** registered variants, sources, findings, correction impacts
   and complete JSON export. Tests retain all 80 registered variants rather than
   exporting selected successes. See [record APIs](records-api.md).
4. **Execution admission:** human public-input releases, project limits,
   cumulative task budgets, grants, bounded worker leases, epochs, observations
   and reconciliation. Unknown termination retains exposure. Review fixed budget
   bypass across repeated grants and plan revisions. See [admission](execution-admission.md).
5. **Worker input delivery:** current lease and release checks precede byte
   delivery; replay rechecks authority. Worker staging checks pinned manifest,
   file set, hashes, sizes, paths and filesystem aliases. See [input delivery](input-delivery.md).
6. **CPU supervision:** durable start intent, stable operation identity, verified
   read-only input mounts, cancellation and monotonic duration enforcement.
   Recovery detects observable clock rollback. Review fixed watchdog abandonment,
   rollback-fence loss and unbounded verification reads.
7. **Runtime integration:** control-issued frozen packages connect to input
   staging and the supervisor. Restart reuses the operation and input tree; a
   lease cannot bind a second local journal under the worker root. Review fixed
   stale-epoch journal poisoning. The HTTP channel binds credentials to one
   origin, denies redirects and enforces a total response deadline with one
   unresolved request per channel. Tests show stalled transport cannot hold up
   cancellation. See [CPU runtime](cpu-runtime.md).
8. **Artifact quarantine:** immutable, checksum-checked, bounded worker outputs,
   including attributable late uploads. Records remain quarantined/unclassified;
   they cannot establish success or settle charges. Review fixed lease-expiry
   classification and BLOB reads in metadata operations. Resumable uploads reserve
   declared bytes and recover committed progress after lost replies; binary chunks
   remain quarantined after checksum verification. Review corrected restart field
   matching, SQLite binary concatenation and metadata queries. Complete output
   manifests now require the exact approved path set and recheck bounded retained
   bytes before recording immutable quarantine metadata. Worker transfer resumes
   after lost manifest responses. See [artifact quarantine](artifact-quarantine.md).
9. **Desktop shell:** isolated renderer, sender-checked IPC, named connections,
   project/draft/approval/labbook views, attributed conversations and explicit
   revision-bound human decision resolution. Review corrected draft carryover, delayed thread
   selection, pagination replacement and overlapping refreshes. Tests execute the
   actual renderer with deferred replies to verify context fences and stable
   message retry keys. Decision tests cover uncertain responses through Refresh,
   shared pending command IDs, and stale context completion. A native sandboxed
   Electron/HTTP fixture passes actual authentication and decision resolution;
   it caught and corrected a `/me` identity-field mismatch hidden by unit mocks.
   Read-only research views now retain all study variants, source versions,
   findings and correction history. The live Electron fixture displays 80 variants
   without changing the ledger. Native JSON export preserves the complete envelope
   on both active and held restored branches. Study creation and variant registration
   now use the same scoped, idempotent command boundary, with authoritative open-study
   checks and context-cleared drafts. Native authoring creates and exports two
   registered variants without asserting execution. Review corrected a stale-read mock signature and
   ensured that returned study/project identity matches the selected context.

10. **Stopped-workspace collector:** declared-path, descriptor-relative reads reject
    links, aliases, special files, cross-device traversal and oversized outputs.
    Independent review found and fixed a hardlink race between preflight and open.
    The low-level reader returns immutable bytes; automatic runtime collection
    remains gated on verified persistent storage and durable sealing.

11. **Human quarantine review:** digest-bound, revision-checked dispositions append
    attributed history. Rejection retains evidence; continued quarantine releases
    nothing. Historical replay remains subject to live credential and project
    access checks. Result acceptance and data release remain unimplemented.

12. **Held recovery:** online SQLite backups preserve records and retained bytes.
    Restores revoke copied credentials and create a fresh owner on a permanently
    read-only branch. Review fixes reject executable schema and changed foreign-key
    or index definitions before publication. Concurrent-write, partial-upload,
    cached-replay and publication-fault tests pass. Native Electron browses the
    restored 80-variant ledger with writes disabled. This does not fence the original
    control service or reconcile its providers; see [recovery](recovery.md).

Each material delegated result was inspected and verified by the primary agent.
Incomplete subagent results were returned for correction; review findings have
regression tests. Successful simulated provider operations are never treated as
real execution receipts or scientific findings.

## Implementation checkpoints

The foundation was merged earlier as `0599b96`. Implementation checkpoints include:

- `8551689`: bounded workspace inventory and reviewed byte capture.
- `2e9261c`: structured harness transport.
- `177c72f`: CPU supervision and verified read-only inputs.
- `6e44d1d`: durable control records, execution authority and artifact quarantine.
- `d987aaa`: frozen control packages connected to the CPU supervisor.
- `00b04c8`: isolated desktop shell and connection boundary tests.
- `466296a`: implemented contracts and remaining acceptance gates.
- `18977cd`: bounded resumable artifact uploads and worker recovery.
- `4d806ba`: desktop conversations and read-only decision inbox.
- `802a06d`: live control-to-Docker and sandboxed Electron smoke checks.
- `d48f038`: bounded stopped-workspace collector and storage admission decision.
- `7ed75ed`: complete quarantined output manifests and resumable transfers.
- `ebe5d8d`: rejected Mac fixed-image backend after Docker execution probe.
- `ae76de5`: desktop decision resolution and live Electron/control acceptance.
- `7efc7b8`: fenced human quarantine review with schema v10.
- `c2bc4c5`: complete read-only study browsing in the desktop.
- `6173b8f`: online backup and held schema-v11 recovery.
- `046303d`: standalone snapshots from WAL-mode sources.

The shared control schema/API changes are one atomic commit so startup and
migration dependencies remain coherent. No license was added.

## Environment checks

Docker Compose builds and tests work. Native Docker access is now available.
`scripts/check_cpu_runtime.py` passed isolation, revocation, and duration probes;
`scripts/check_control_runtime.py` passed real HTTP package/input delivery and
control revocation with actual provider observations. The isolation probe checks
usable interfaces and absence of routing, accommodating dormant tunnel devices.

Locked Electron installation now succeeds. The native in-process smoke passes
without disabling the sandbox: actual URL, bridge, load state, lack of renderer
Node access, and absence of renderer errors are checked. Visual QA and signed
packaging remain unvalidated.

Git publication works again. The previous nine pending commits were pushed and
CI passed through `b1c4adc`. No force push or authentication reconfiguration was used.

Persistent container output quotas are not verified on this Mac's Docker Desktop.
A fixed-size HFS+ disk-image probe failed the execution restriction: Docker's
virtiofs share omitted host `noexec` and executed a workspace script successfully.
The temporary fixture was removed; this backend remains inadmissible. See the
[storage decision](decisions/0003-persistent-output-storage.md) for evidence and
cleanup limits. The target execution host and
representative research question/dataset remain open configuration inputs.

## Remaining milestones

1. Verified persistent output storage, durable sealing and runtime collection,
   quarantine release policy, and fenced authoritative result acceptance.
2. Worker trust/build enrollment, capability probes, registration/drain/retirement,
   credential brokering and operational spend/incident reconciliation.
3. Pinned runtime skill authoring/test/release and mediated harness integration.
4. Desktop adoption, skills, research editing, broader reproduction exports, telemetry and
   stop/edit/handback with writer quiescence; portable artifact bundles.
5. Operational restore fencing/resumption, lifecycle recovery, cleanup/retention, packaging and
   the remaining adversarial acceptance fixtures in DESIGN.md section 8.
6. A maintainer-selected real investigation and measured pilot, including trusted
   signing, identity and infrastructure configuration where required.
7. Additional runtimes, GPU/cluster targets, organization/hosted modes and other
   deferred design features after their prerequisite acceptance gates.

Unattended research dispatch remains disabled pending its integration gates.
