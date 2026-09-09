# Implementation progress

This file tracks executable evidence against DESIGN.md. Initial development uses
logical commits on main; PRs remain paused. The full design and real research
pilot are not complete.

## Design status matrix

Reviewed 2026-09-09 against schema v23 reconciled private project-artifact persistence and
the schema v21 metadata-only credential-access lease,
v20 bounded human result-bookkeeping ledger, v19 fake-provider receipt slice, v18 external-operation journal, v17 bounded confirmed workspace handoff, v16 skill lifecycle and bounded
cancellation telemetry projection, synthetic
OpenCode harness adapter POC, and bounded capability-probe slice. These are
informational evidence only, not live integration, cryptographic trust,
capability attestation, dispatch, or pilot completion.
**Implemented** means the named bounded behavior has executable evidence;
**partial** means the design workflow still has missing requirements; **blocked**
identifies an unmet prerequisite; **deferred** is intentionally outside the pilot.
No entire pilot workflow should be marked complete from component tests alone.

| Design contract | Status | Implemented evidence/scope | Remaining acceptance work |
|---|---|---|---|
| §2.1 Start/adopt | Partial | Bounded inventory, retained snapshots, releases, desktop projects/studies | Desktop adoption workflow and imported-work continuity |
| §2.2 Runtime skills | Partial | Metadata-only immutable snapshots, human worker releases and release-fenced pin admission | Skill editor/testing and mediated loading; local CPU packages reject pins because materialization/verification and a harness adapter remain unimplemented |
| §2.3 Plan/approve/execute | Partial; real output integration blocked | Immutable plans, human decisions, budgets, grants, leases, v20 human bookkeeping over v19 fake-provider receipt evidence and v18 durable package intents, CPU supervisor, live HTTP/input fixtures, an explicit development-only native one-shot worker, and v23 explicit project-object placement after quarantine | Decisive Linux Docker-host byte/inode exhaustion and restart evidence, backend-enforced worker-slot storage and automatic production collection, real provider dispatch/accounting and broader authoritative result policy; approval does not launch a job |
| §2.4 Discuss/steer | Partial | Durable threads, messages, typed decision inbox, explicit human resolution, desktop cancellation status labels | Full telemetry, queue-pressure behavior, integrated agent steering |
| §2.5 Stop/takeover/handback | Partial | CPU cancellation, revocation/duration enforcement, cancellation attempts independent of provider/control report failures, restart-safe development heartbeats, bounded requested/unresolved/worker-confirmed termination telemetry, and schema v17 confirmed handoff/digest diff/fresh-context fence | Arbitrary shared-writer mediation, full provider quiescence, desktop adoption and full telemetry milestones |
| §2.6 Labbook | Partial | Complete bounded study ledger, all variants, attributed events, desktop browsing | Broader investigation navigation and linked execution/report history |
| §2.7 Review/export | Partial | Source correction; human accept/reaffirm/narrow/supersede/retract; all versions retained; consistent study JSON | Impact propagation beyond sources/findings; portable reproduction bundle with code, artifacts, reports and instructions |
| §§3–5 Architecture/authority | Partial | Separate control/desktop/worker components, scoped credentials/data access, isolated renderer with context-fenced drafts, immutable input delivery, owner-managed worker registration/lifecycle, schema v21 human-bound metadata-only credential-access leases with short-lived fake handles, documented optional pinned-harness boundary, synthetic OpenCode authority-rechecked contiguous replay/cancellation POC, and a read-only informational capability probe | Live OpenCode compatibility/security testing, enrolled-node integration, cryptographic build trust and capability attestation, production secret backend/real provider credential integration, dispatch, and broader operational identity lifecycle |
| §6 Execution/recovery | Partial | Durable external-operation intent/observations, stable operations, conservative exposure, quarantine, resumable transfer, schema-v23 project-object intent/reconciliation, and read-only held restore | Provider fencing/resumption after restore, portable object-bundle recovery, release/result policy, spend and incident reconciliation |
| §7 Scientific integrity | Partial | Registered/accounted variants, source support gates, explicit human judgments and review history | Protected evaluation, stronger independence profiles, method-specific validation and real scientific acceptance |
| §8 E2E acceptance | Blocked for real pilot; component work continues | Native CPU/control and sandboxed Electron fixtures; migration/rollback and adversarial component tests | Admissible execution storage/host, selected investigation/dataset, remaining failure drills and measured pilot |
| §9 Operations/product | Partial | Pinned development dependencies, CI, explicit migrations, backup/held-restore runbook | Signed packaging, update/drain/rollback, retention, monitoring, operational recovery and pilot measurements |
| §1 post-pilot/later scope | Deferred | Contracts only where explicitly documented | Additional harnesses/runtimes, GPU/cluster, organization/hosted modes after prerequisite gates |

### Keeping status current

For each logical implementation commit, update its matrix row and the relevant
reviewed step below: implemented behavior, missing behavior, test/fixture evidence,
and any blocker. Record the tested commit and schema version; test counts are a
dated snapshot, not live badges. Link supporting API/runbook/decision documentation.
Keep DESIGN.md as the target contract; change it when the intended contract changes,
not merely to make incomplete work look complete. Mark E2E complete only after the
full workflow and its required adverse cases pass on the supported target.

## Verified evidence

- Current schema-v23 remediation checkpoint: 538 Python tests pass natively,
  including cancellation/report-failure handling, restart/heartbeat replay,
  v22 `staged_at` migration, project-object integrity and zero-output handling,
  acceptance retraction after object corruption, OpenCode authority/replay fencing,
  and migration rollback/evidence preservation. This remains development/synthetic
  evidence only.
- Ruff lint and format checks pass.
- 65 desktop connection/IPC and renderer tests pass using the locked desktop
  dependencies.
- Explicit migrations through schema v23 preserve historical records, input bytes,
  active grants, leases, v22 project placements and held exposure; injected
  migration faults roll back.
- Native CPU, HTTP/control and development-worker Docker fixtures pass. The
  development fixture persists declared output to project storage after quarantine;
  it remains synthetic and does not establish pilot storage or result acceptance.
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
    after lost manifest responses. A synthetic-only private SQLite seal journal
    now requires a fresh matching termination observation before descriptor-relative
    collection, retains immutable bytes/digests and its manifest before transfer,
    and resumes a lost upload/manifest response after worker restart. It is not
    connected to a real CPU runtime or storage backend. Explicit local
    development retention now has a separate marked-workspace helper, which
    remains quarantine-only and does not satisfy backend activation or scientific
    acceptance. An opt-in test-facing local CPU seam now reserves an
    operation-specific private development workspace with userspace byte/inode
    accounting, refuses unsafe or undeclared post-stop nodes, requires a fresh
    matching confirmed termination before sealing, and reclaims only after
    quarantine transfer. It is not wired into normal `CpuRuntime`, package
    admission, persistent storage validation, dispatch, release, acceptance, or
    scientific claims. See
    [artifact quarantine](artifact-quarantine.md).
9. **Desktop shell:** isolated renderer, sender-checked IPC, named connections,
   project/draft/approval/labbook views, attributed conversations and explicit
   revision-bound human decision resolution. Review corrected project/task/thread,
   connection, and research-record draft carryover, delayed thread
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
   variants, records their failed/abandoned outcomes, closes the accounted study,
   and exports both without asserting execution. The native evidence flow also
   checks source assessment, unsupported-citation blocking, explicit acceptance,
   correction history and downstream review. Review corrected a stale-read mock signature and
   ensured that returned study/project identity matches the selected context.

10. **Stopped-workspace collector:** declared-path, descriptor-relative reads reject
    links, aliases, special files, cross-device traversal and oversized outputs.
    Independent review found and fixed a hardlink race between preflight and open.
    The low-level reader returns immutable bytes; automatic runtime collection
    remains gated on verified persistent storage and durable sealing.

    **Persistent output-storage contract:** a new package can explicitly request
    version 1 capability evidence for fixed byte/inode capacity, mount/filesystem
    identity, execution restrictions, UID 65532 write access, custody, and no
    nested mounts. Synthetic contract tests fail closed and freeze validated
    identity/capacity into the package digest. This does not activate a backend,
    mount storage, reserve a slot, collect automatically, release quarantine, or
    accept a result.

    **Read-only backend inspection:** a separate worker probe inspects an
    already-provisioned dedicated Linux mount without mutation or subprocesses.
    It rejects missing, linked/replaced, unsafe-custody, unidentifiable,
    unrestricted, nested, invalid-capacity, or UID 65532-inaccessible paths and
    returns the existing canonical contract only on success. macOS Docker Desktop
    and unknown platforms remain non-admissible; Linux Docker-host exhaustion and
    restart fixtures are still required before backend activation.

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

13. **Finding reaffirmation:** humans can explicitly reaffirm unchanged findings
    after their existing citations regain support. Reasons and review actors enter
    the durable ledger; superseded citations block reaffirmation. API, desktop,
    export, and held-restore fixtures cover the transition. Revision and retraction
    support are described below.

14. **Finding retraction:** revision-bound human retraction preserves statements,
    references, original acceptance attribution, and review history. Terminal
    findings cannot be reactivated by later source corrections. Schema 12 upgrades
    preserve citation tables transactionally; migrated records survive held restore.

15. **Linked finding revisions:** narrowing and supersession retain the original
    finding and create an explicitly cited candidate, including for closed studies.
    Replacement acceptance is a separate review. Schema 13 preserves linked versions,
    reasons, attribution, and original evidence through export and held restore.

16. **Worker lifecycle:** schema v14 binds one existing scoped worker credential
    to approved metadata, a project, lifecycle state, and heartbeat metadata.
    Only fresh `ready` workers can acquire or renew leases; drain/quarantine
    blocks new authority and retirement revokes the workload credential while
    retaining exposure and observations. This is not build attestation or a
    capability claim. See [worker lifecycle](worker-lifecycle.md).

17. **Optional OpenCode harness adapter POC:** an injected in-process client
    proves exact-version rejection, authority rechecks at each privileged boundary,
    bounded synthetic session/message translation, full event attribution/quarantine,
    contiguous per-operation deduplication, restartable exclusive-cursor replay,
    prompt observation, and unresolved-to-confirmed cancellation. Uncertain upstream
    effects retain one operation identity instead of creating a dishonest replay.
    It has no live OpenCode/provider call, control write, dispatch,
    artifact/result acceptance, credential path, compatibility result, or pilot
    completion. See [the adapter note](opencode-harness.md).

18. **Synthetic result checkpoint:** schema v15 records a human-only immutable
    checkpoint after a reconciled, worker-confirmed exit, exact operation/epoch,
    complete non-late quarantined manifest, and deterministic retained-byte/metadata
    validation. It preserves quarantine and labels both synthetic validation and
    trusted-worker receipt limitations; it does not accept findings or provider
    success. See [execution admission](execution-admission.md).

19. **Bounded skill lifecycle:** schema v16 persists deterministic manifest,
    metadata and digest snapshots from non-executing validation, then human-only
    worker releases with expiry/revocation. Pin admission freezes and rechecks the
    release; local CPU package/runtime loading remains explicitly rejected. See
    [skill lifecycle](skill-lifecycle.md).

20. **Synthetic capability probe:** bounded immutable reports compare exact
    supplied runtime/schema versions, inspect private 0700 roots, validate an
    optional declared storage description as unverified, and observe an optional
    pinned OpenCode version. Callback failures fail closed. Reports cannot
    attest builds, grant authority, update the registry, or enable dispatch.

21. **Confirmed workspace handoff:** schema v17 binds a human stop request to one
    current task/grant/lease and captured baseline, fences the grant, and requires
    a matching stable-provider worker-confirmed exit before recording human ownership.
    Handback records immutable result capture digests, path diff/exclusions and a
    new context epoch; it cannot dispatch, accept results, or mediate arbitrary
    writers. See [workspace handoff](workspace-handoff.md).

22. **Stage 1 development native worker:** `tessera-worker development-run`
    consumes only an already issued grant and a private project-scoped worker
    credential file. It uses distinct durable heartbeat intents, heartbeats before
    lease acquisition and renewal, verifies the frozen CPU package and local
    development output marker, reuses persisted renewal/package-read keys after
    lost responses, and reuses the supervisor start journal on restart. Provider,
    observation, heartbeat, or renewal report failure still attempts cancellation.
    It reports observations, seals/uploads declared confirmed-exit output into
    quarantine only, and removes its workspace only after transfer. It cannot
    self-register, issue/reconcile human authority, release artifacts, commit a
    result checkpoint, or claim provider/scientific success. The loopback Docker
    fixture is development evidence only; Linux hard quota/restart and Minikube
     evidence remain open. See [decision 0005](decisions/0005-execution-backend-profiles.md).

23. **External-operation journal:** schema v18 atomically records the one
    development-local CPU operation before returning its executable package,
    fences package/observation identity and immutable provider ID, and updates
    desired cancellation with grant revocation. Tick exhaustion requests
    cancellation instead of abandoning Docker; terminal restart recovery reads
    provider state without restart. v17 leases remain null/unbackfilled legacy
    records. It is not dispatch, a provider receipt, release, result acceptance,
     or scientific success. See [the journal note](external-operation-journal.md).

24. **Fake-provider receipt slice:** schema v19 stores a separate immutable
    fake-provider receipt after local start intent/authority recheck, uses stable
    lookup reconciliation after lost submits, and retries receipt/observation
    reports with durable identities. It preserves Docker adapter behavior and
    quarantine-only output, but is not real provider dispatch/accounting, pilot
    acceptance, artifact release, result acceptance, or scientific success.

25. **Bounded human result bookkeeping:** schema v20 appends a revision-fenced
    human accept/reject/retract ledger over exact reconciled synthetic checkpoint,
    fake receipt, operation, lease/epoch, manifest and retained bytes. It preserves
    quarantine, findings, costs, and explicit not-assessed/not-established/not-
    performed labels. Retraction remains fenced to the prior decision but possible
    after accepted project bytes are later missing or corrupt; it is not real
    provider/scientific acceptance or release.

26. **Credential-access leases:** schema v21 records human-only opaque-reference
    leases bound to the current task/grant/execution lease/worker/epoch and returns
    only short-lived handles to authenticated workers. The private in-memory fake
    backend rechecks authority at resolve and records no values in SQLite, events,
    API responses or diagnostics. It is not a production secret backend or real
    provider credential integration; see [decision 0007](decisions/0007-credential-access-lease-boundary.md).

27. **Project artifact storage:** schema v23 records durable intent before backend
    stage/commit/reconciliation and commits SQLite placement authority only after
    backend integrity verification. Recoverable stale publication hard links are
    repaired outside authority transactions; manifest/checkpoint/acceptance
    integrity inspection does not repair backend state. Schema-v22 `staged_at`
    operations and placements migrate without invented legacy locations. Explicit
    zero-output placement is recorded without an invented object.
    Placement stays quarantined and has no finding, classification, cost, provider,
    or scientific effect. Legacy BLOBs remain unplaced. S3/IAM/KMS, Linux
    worker-slot enforcement, export/release, object-bundle restore/deletion, and
    pilot acceptance remain open.

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
- `b1c4adc`: native complete study JSON export.
- `f90eda8`: desktop study creation and variant registration.
- `878a01a`: recorded outcomes and accounted study closure.
- `ddae748`: evidence review, explicit acceptance and source correction.
- `f674603`: reasoned finding reaffirmation and project-scoped record access.
- `47383a4`: terminal finding retraction and preserving schema 12 migration.
- `ee6c36d`: linked narrowing/supersession, schema 13 and native export/restore integration.
- `175a5b3`: schema v14 worker registration, liveness admission and retirement revocation.
- `9967b50`: schema v16 bounded skill lifecycle.
- `b70772b`: schema v17 bounded confirmed workspace handoff.
- `0a00661`: Stage 1 development worker E2E.
- `6b59112`: schema v18 development-local external-operation journal.
- `3f43fe2`: schema v19 fake-provider receipt dispatch.
- `0756150`: schema v20 bounded result-acceptance ledger.
- `b7e304c`: schema v21 credential-access leases.
- `b67807a`: schema v22 project-artifact storage.
- Current checkpoint: integrated lifecycle, schema-v23 storage, OpenCode replay,
  and desktop draft-fencing remediations.

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
CI passed through `ee6c36d`. No force push or authentication reconfiguration was used.

Persistent container output quotas are not verified on this Mac's Docker Desktop.
A fixed-size HFS+ disk-image probe failed the execution restriction: Docker's
virtiofs share omitted host `noexec` and executed a workspace script successfully.
The temporary fixture was removed; this backend remains inadmissible. See the
[storage decision](decisions/0003-persistent-output-storage.md) for evidence and
cleanup limits. The target execution host and
representative research question/dataset remain open configuration inputs.

## Remaining milestones

1. Verified persistent output storage and real runtime collection, quarantine
    release policy, and broader real-provider/method result acceptance. Synthetic
    sealing, checkpoints, fake receipts and bookkeeping are not backend activation
    or real pilot acceptance.
2. Cryptographic worker build trust, capability probes, credential brokering and
   operational spend/incident reconciliation. Registration/drain/quarantine/
   retirement and bounded heartbeat admission are implemented; see
   [worker lifecycle](worker-lifecycle.md).
3. Pinned runtime skill authoring/test/release and mediated harness integration.
4. Desktop adoption, skills, broader reproduction exports, full telemetry and
   arbitrary writer mediation/full stop-edit-handback adoption; portable artifact bundles.
5. Operational restore fencing/resumption, lifecycle recovery, cleanup/retention, packaging and
   the remaining adversarial acceptance fixtures in DESIGN.md section 8.
6. A maintainer-selected real investigation and measured pilot, including trusted
   signing, identity and infrastructure configuration where required.
7. Additional runtimes, GPU/cluster targets, organization/hosted modes and other
   deferred design features after their prerequisite acceptance gates.

Unattended research dispatch remains disabled pending its integration gates.
