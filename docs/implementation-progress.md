# Implementation progress

This file tracks executable evidence against DESIGN.md. Initial development uses
logical commits on main; PRs remain paused. The full design and real research
pilot are not complete.

## Verified evidence

- 263 Python tests pass natively and in the isolated Docker Compose test service.
- Ruff lint and format checks pass.
- Sixteen desktop connection/IPC and renderer tests pass using Node 22.12.
- Explicit migrations through schema v8 preserve historical records, input bytes,
  active grants, leases and held exposure; injected migration faults roll back.
- No tests were skipped. Dependency deprecation warnings remain.

These are component and API integration results. Container execution, Electron
runtime/visual QA, scientific output validation and a representative investigation
have not passed end-to-end acceptance.

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
   matching, SQLite binary concatenation and metadata queries. See [artifact quarantine](artifact-quarantine.md).
9. **Desktop shell:** isolated renderer, sender-checked IPC, named connections,
   project/draft/approval/labbook views, attributed conversations and a read-only
   pending decision inbox. Review corrected draft carryover, delayed thread
   selection, pagination replacement and overlapping refreshes. Tests execute the
   actual renderer with deferred replies to verify context fences and stable
   message retry keys. Research-ledger views and decision resolution remain absent.

Each material delegated result was inspected and verified by the primary agent.
Incomplete subagent results were returned for correction; review findings have
regression tests. Successful simulated provider operations are never treated as
real execution receipts or scientific findings.

## Local commits

The foundation was merged earlier as `0599b96`. Git commit access now works and
these additional implementation commits are saved locally:

- `8551689`: bounded workspace inventory and reviewed byte capture.
- `2e9261c`: structured harness transport.
- `177c72f`: CPU supervision and verified read-only inputs.
- `6e44d1d`: durable control records, execution authority and artifact quarantine.
- `d987aaa`: frozen control packages connected to the CPU supervisor.
- `00b04c8`: isolated desktop shell and connection boundary tests.
- `466296a`: implemented contracts and remaining acceptance gates.
- `18977cd`: bounded resumable artifact uploads and worker recovery.

The shared control schema/API changes are one atomic commit so startup and
migration dependencies remain coherent. No license was added.

## Environment checks

Docker Compose builds and tests work. The native CPU smoke script still cannot
access the Docker socket, so its real isolation/cancellation fixtures remain
unvalidated. No alternate socket mount or privileged workaround was used.

Electron installation was retried with supported Node 22.12 and failed fetching
`sumchecker` from the npm registry with DNS `ENOTFOUND`. Desktop launch, visual QA
and signed packaging remain unvalidated.

Git staging and committing work. Fetch still fails writing `.git/FETCH_HEAD`, and
the remote read check failed resolving `ssh.github.com`. Remote publication is
pending; no force push or remote/authentication reconfiguration is used.

## Remaining milestones

1. Bounded container collection, manifest completion,
   validation/quarantine disposition and fenced authoritative result acceptance.
2. Worker trust/build enrollment, capability probes, registration/drain/retirement,
   credential brokering and operational spend/incident reconciliation.
3. Pinned runtime skill authoring/test/release and mediated harness integration.
4. Desktop adoption, skills, research views, decision resolution, telemetry and
   stop/edit/handback with writer quiescence; portable artifact bundles.
5. Backup/restore fencing, lifecycle recovery, cleanup/retention, packaging and
   the remaining adversarial acceptance fixtures in DESIGN.md section 8.
6. A maintainer-selected real investigation and measured pilot, including trusted
   signing, identity and infrastructure configuration where required.
7. Additional runtimes, GPU/cluster targets, organization/hosted modes and other
   deferred design features after their prerequisite acceptance gates.

Unattended research dispatch remains disabled pending its integration gates.
