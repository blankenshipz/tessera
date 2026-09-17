# Tessera implementation tasks

Created: 2026-09-15. Based on the 2026-09-14 comprehensive source review of
`main` at `6668ab8` plus the uncommitted schema-43 working tree.

## Objective

A researcher can approve a bounded investigation, turn their laptop off, and
return to the same remote run, retained evidence, outstanding decisions, and a
human-reviewed conclusion. Control, coordinator, credentials, storage, and
execution must remain available independently of the laptop.

This is an implementation backlog, not a statement that these features are
complete. Follow [DESIGN.md](DESIGN.md) for contracts and
[CONTRIBUTING.md](CONTRIBUTING.md) for verification and operational scope.
Maintain [implementation progress](docs/implementation-progress.md) as the
capability/evidence record; use this file to track remaining work.

## Tracking and completion

- Tasks start unchecked. Mark complete only with acceptance evidence.
- Record the tested commit/tree, tests or fixture report, remaining limitations,
  and relevant documentation under the task when closing it.
- Recheck the current implementation before acting on a dated finding. Preserve
  existing uncommitted work and keep changes scoped.
- Default tests use temporary state and fake effects. Keep real infrastructure
  and provider runs in separately enabled fixtures with bounded usage and the
  required operational authorization. An implemented harness is not a live pass.
- A task pass is not full integration or pilot acceptance. Do not carry historical
  schema-42 test counts forward as evidence for schema 43 or later.

## Phase 1 — Correctness and a reproducible baseline

### T01 — Enforce Kubernetes authority expiry locally [High; review F1]

- [x] Bound supervision by the control-issued `hard_expires_at`, current lease
  expiry, approved duration and disconnect policy; preserve bounds on restart.
  Validate renewal identity and expiry. Prevent delayed startup from extending
  duration, and align provider-side deadlines with the admitted bounds.
- Areas: `src/tessera_worker/kubernetes_dispatch_service.py`,
  `src/tessera_control/runtime_packages.py`, `src/tessera_control/execution.py`.
- Acceptance: a 300-second task with a 20-second grant cannot remain authorized
  beyond the grant when control is unavailable; delayed startup and restart do
  not reset the deadline. Existing valid renewal behavior still passes.
- Review evidence: fake Kubernetes remained running at second 24, about four
  seconds beyond control hard expiry, with a local 300-second deadline.
- Current focused evidence (2026-09-15): `.venv/bin/pytest -q
  tests/test_kubernetes_dispatch.py tests/test_kubernetes_dispatch_recovery.py
  tests/test_kubernetes_execution.py` passed 60 tests, including hard-expiry, delayed-start,
  restart, renewal, and provider-deadline cases. This is fake-client evidence;
  no live cluster or remote deployment was tested.
- Astra repair evidence (2026-09-15): the focused dispatch/remote-workflow rerun
  passed 50 tests, including in-process monotonic expiry during wall-clock rollback,
  restart rollback detection, hard/outage/lease precedence, and one-Job recovery.

### T02 — Keep supervising after Kubernetes observation errors [High; review F3]

- [x] Handle mid-loop Job/Pod read failures as unknown observations while retaining
  responsibility for expiry, cancellation, reporting and reconciliation of the
  original operation. Cover both normal polling and control-outage branches.
- Areas: `src/tessera_worker/kubernetes_dispatch_service.py`,
  `src/tessera_worker/kubernetes_execution.py`.
- Acceptance: a read error after submit does not escape the supervising loop or
  create a replacement Job; simultaneous control/cluster outage still enforces
  local bounds and retains unresolved termination until supported by evidence.
- Review evidence: a post-submit `ConnectionError` escaped `run()` with the fake
  Job present, local state unknown, and cancellation not requested.
- Current focused evidence (2026-09-15): that 60-test dispatch/execution command
  passed the normal and outage recovery cases, including Job/Pod read
  errors, retained ownership, cancellation, and no replacement create. This is
  synthetic callback evidence only.

### T03 — Maintain worker registration heartbeats [High; review F2]

- [x] Send durable, replay-safe registry heartbeats during supervision and on
  recovery before seeking continued authority. Preserve separate worker-health
  and execution-lease semantics; do not weaken freshness checks.
- Areas: `src/tessera_worker/kubernetes_dispatch_service.py`,
  `src/tessera_control/workers.py`, `tests/test_kubernetes_dispatch.py`.
- Acceptance: run worker and control on a shared test clock past the 60-second
  freshness threshold; valid work continues, genuinely stale workers fail closed,
  and lost heartbeat replies/restarts reuse pending keys safely.
- Review evidence: a valid 300-second job cancelled at second 61 after one
  heartbeat and six successful lease renewals.
- Current focused evidence (2026-09-15): `.venv/bin/pytest -q
  tests/test_workers.py tests/test_kubernetes_dispatch.py -k 'heartbeat or fresh or
  stale or renewal'` passed 16 tests (28 deselected), including shared-clock freshness,
  fail-closed behavior, lost heartbeat replay, and restart-safe keys. This does
  not establish a live worker or cluster heartbeat.

### T04 — Integrate schema 43 and repair migration checks [High; review F4]

- [x] Reconcile current-version migration/backup assertions with the current
  schema, retaining literal historical versions where intentional. Add direct
  v42-to-v43 preservation, repeatability, injected rollback and held-restore tests.
- Areas: `src/tessera_control/migrations.py`, migration/recovery tests,
  `tests/test_remote_workflows.py`.
- Acceptance: current-schema checks pass; migration creates no invented historical
  runs or authority, preserves existing evidence, and rolls back completely on
  failure. Updating expected numbers alone is insufficient.
- Review evidence: focused Python checks returned 45 passed/7 failed; the seven
  failures expected 42 but migration/backup returned 43.
- Current evidence (2026-09-15, dirty working tree based at `6668ab8`): `.venv/bin/pytest
  -q tests/test_execution_migration.py tests/test_records_migration.py
  tests/test_artifact_migration.py tests/test_finding_migration.py
  tests/test_snapshot_migration.py tests/test_recovery.py tests/test_recovery_safety.py
  tests/test_remote_workflows.py` passed 42 tests, including
  current-schema assertions, preservation and repeatability, rollback, and held
  restore. The historical 45 passed/7 failed result remains review evidence and
  is not carried forward.
- Projection repair evidence (2026-09-15): raw worker/provider samples remain in
  their operational journals while remote-workflow state follows observed-time
  and receipt-sequence reconciliation. Focused tests cover delayed cross-source
  arrival and fail-closed equal-time contradiction.

### T05 — Correct capability and backup documentation [Medium; review F8]

- [x] Align README, quickstart, changelog, privacy, decision 0008, compatibility
  matrix and progress notes with actual current behavior. Explain that local
  object bundles copy committed project bytes and restore to held history;
  operational failover remains unavailable. Separate fixtures, opt-in cluster
  tools, supported runtime paths and planned features. Preserve dated evidence.
- Acceptance: no conflicting current-schema claims or inaccurate backup custody
  statements; supported remote execution is not implied by an operator fixture.
- Evidence (2026-09-15, uncommitted working tree): updated the seven scoped
  documents plus implementation progress to identify schema v43, versioned local
  object-bundle custody and permanently read-only held restore, and the
  provider-disabled, synthetic, opt-in/live, and unsupported-failover boundaries.
  Documentation consistency scans and `git diff --check` passed; no source,
  desktop behavior, `docs/cpu-runtime.md`, credential, or commit changes were
  made for T05.
- Current documentation checks (2026-09-15) also found no broken relative
  Markdown links in the two files changed here; `git diff --check` passed.

### T06 — Establish the integration checkpoint

- [x] After T01–T05, run the repository's locked integration gates: Ruff lint,
  formatting, Python tests and desktop Node tests. Record the exact tree and
  environment, failures/unrun gates, and focused regression evidence.
- Acceptance: all standard gates pass on the identified tree; no blanket live
  provider, remote execution or scientific-validity claim follows from this pass.
- Current evidence (2026-09-15): on Python 3.11.6 / pytest 9.1.1, `.venv/bin/ruff
  check .` passed, `.venv/bin/ruff format --check .` passed (251 files),
  `.venv/bin/pytest` passed (917 passed, 2 deprecation warnings), and
  `npm test` in `desktop/` passed (87 passed, 0 failed, 0 skipped). The tested
  tree was the dirty working tree at `6668ab8789c1fc5c99f9b09b94cb8f4d5e1f23b6`
  (HEAD); its uncommitted source, test, and documentation edits are not
  represented by a commit ID, so this is not a clean-commit claim. The native
  Electron/control fixture also passed active and held-restore flows, displayed
  all 80 synthetic variants, matched study exports, and reported
  `execution_started=false`.
  The gates provide no live Kubernetes, provider, remote-execution, or scientific
  acceptance evidence. Pytest emitted the existing Starlette/httpx and anyio
  deprecation warnings; no test failures or skips were observed.

## Phase 2 — An always-on remote backend and agent

### T07 — Select the first remote deployment and research workflow

- [ ] Specify one researcher, public input set, baseline/intervention, meaningful
  measurement and review criterion. Name the backend, worker, harness/model,
  credential custody, artifact storage and operator. Define expiry, outage,
  cancellation, cost/exposure, storage and recovery bounds.
- Dependencies: planning can proceed during Phase 1; runtime activation follows
  the applicable correctness and profile gates.
- Acceptance: the deployment diagram contains no laptop dependency after input
  upload/admission, including tunnels, login refresh, keychains or local volumes.
  Document unresolved choices rather than inventing target/provider availability.

### T08 — Supply an explicit remote control-service deployment [High; review F6]

- [ ] Implement configuration/packaging for the selected always-on service:
  trusted encrypted endpoint, host validation, persistent state, process lifecycle,
  health, version compatibility and authenticated desktop/worker access. Preserve
  explicit local mode. Keep a single authoritative home for each project.
- Areas: `src/tessera_control/api.py`, `cli.py`, deployment configuration/runbooks.
- Depends on: T06, T07 for acceptance.
- Acceptance: ordinary clients reach the selected backend securely; Electron exit
  has no effect on service lifetime; restart retains state; wrong identity/host
  and unauthenticated access are rejected. No laptop relay is required.

### T09 — Run the coordinator as a backend consumer [High; review F5]

- [ ] Host durable Astra turn claiming/completion in an independent service with
  scoped identity, health, bounded retries and recovery. Show clearly when no
  coordinator is available. Preserve the deterministic mode as a separate profile.
- Areas: `src/tessera_agent/coordinator.py`, `src/tessera_control/astra*.py`,
  service startup and desktop availability/status handling.
- Depends on: T06; remote acceptance also requires T08.
- Acceptance: a fresh desktop request receives an answer using normal processes,
  without fixture-seeded completions. Consumer restart does not duplicate effects
  or lose queued turns; closing Electron does not stop consumption.

### T10 — Complete remote worker enrollment and lifecycle

- [ ] Package/enroll the selected trusted supervisor with separate workload
  identity, verified build/profile and capabilities, persistent journal custody,
  health, drain/quarantine/retire and restart behavior.
- Areas: worker registry, `src/tessera_worker/`, selected operator/runbook.
- Depends on: T07, T08.
- Acceptance: new/replaced workers require explicit enrollment; stale/retired
  identity cannot obtain new authority; restart reconciles the original operation
  before further effects. Remote identity is not a copied human bearer login.

### T11 — Implement remote credential custody and renewal

- [ ] Connect one real credential backend/delivery path to the selected runtime.
  Design unattended scoped renewal under existing human authorization; do not
  require a new manual action for every short lease. Preserve expiry, revocation,
  owner binding, audit metadata and explicit billing mode.
- Areas: `src/tessera_control/credential_leases.py`, delivery adapters and runtime.
- Depends on: T07, T08, T10.
- Acceptance: approved work can refresh access without the laptop; revoke/expiry
  blocks renewal; outage fails closed; raw values do not enter prompts, records,
  logs or artifacts. No silent account, API-key or billing fallback occurs.

### T12 — Activate one real harness and execution profile

- [ ] Connect the coordinator to one pinned real harness/model and mediated
  experiment adapter. Validate exact version/schema, server trust, tool/network
  limits, attributed events, stable submit lookup, cancellation and usage evidence.
- Areas: agent coordinator, OpenCode/harness adapters, execution adapter/profile.
- Depends on: T01–T03, T07–T11 and applicable output gates in T13.
- Acceptance: a fresh request performs a useful bounded computation and reports
  attributable evidence. Repeated status reads alone do not count as research
  execution. Synthetic/provider-disabled records remain separately labeled.

## Phase 3 — Complete the researcher journey

### T13 — Finish output enforcement, collection and custody

- [ ] Validate the selected remote storage profile's hard byte/inode bounds,
  mount/isolation requirements and restart behavior. Connect declared-output
  collection, resumable quarantine upload, exact manifest and durable project
  placement before reclaiming the worker workspace.
- Areas: worker storage/output modules, artifact/manifest/project storage modules.
- Depends on: T07, T10; storage conformance can precede real harness activation.
- Acceptance: full storage, unsafe paths, incomplete/corrupt output and interrupted
  transfer cannot produce a false complete manifest or lost accepted custody.
  Real target conformance evidence is distinct from userspace fixture accounting.
- Focused conformance-harness safety evidence (2026-09-16, dirty tree): the
  opt-in Astra Kubernetes quota probe now refuses public or symlinked evidence
  parents and writes its target/resource evidence with directory-descriptor,
  no-follow, exclusive, complete-write, and fsync handling. `.venv/bin/pytest -q
  tests/test_astra_kubernetes_conformance_runner.py` passed 12 tests. The live
  probe stopped at validation with `--kubeconfig must name an existing absolute
  file`, before any Kubernetes or provider effect; no selected target was supplied
  to this checkout. This is harness-hardening evidence, not storage quota,
  artifact-custody, or remote-target acceptance.

### T14 — Expose remote progress and reconnect history [Medium; review F7]

- [ ] Finish the typed `remoteWorkflows` preload bridge and renderer projection.
  Show freshness, last checkpoint, outage/reconciliation state and missed decisions
  within the investigation; keep raw identifiers in diagnostics.
- Areas: `desktop/preload.cjs`, `main.mjs`, `connection.mjs`, `renderer.mjs` and tests.
- Acceptance: actual renderer tests cover disconnect/reopen and loading the same
  operation's history; no stale cross-project/connection state or replayed command.
  An opaque checkpoint reference is not labeled resumable without resume support.

### T15 — Join proposal, admission, execution and human review

- [ ] Provide one readable desktop flow from goal and uploaded inputs through
  baseline/intervention proposal, explicit scope approval and separate execution
  admission to remote work, retained measurements and a human conclusion. Connect
  the study/finding and experiment/conclusion representations clearly.
- Depends on: T09–T14.
- Acceptance: complete the workflow without direct database setup or test-only
  completion calls; source/evidence links and negative outcomes remain visible;
  approval, dispatch, custody and scientific review remain distinct operations.

### T16 — Integrate steering, decisions and stop behavior

- [ ] Connect supported contextual messages to the running agent; distinguish
  queued/delivered/answered and requested/applied changes. Hold dependent work when
  new authority is needed. Show stop request versus confirmed termination and
  unresolved exposure. Retain a confirmed-stop handoff boundary for human edits.
- Depends on: T09, T12, T14, T15.
- Acceptance: absence of a human never grants additional authority; questions do
  not silently alter execution; cancellation survives lost replies and reconnect;
  unknown writer quiescence cannot authorize shared writable takeover.

### T17 — Deliver a useful reproducibility export

- [ ] Extend the bounded ledger export with the selected workflow's authorized
  code/environment references, inputs/manifests, retained output, measurements,
  review/correction history and reproduction instructions. State missing or
  restricted material explicitly.
- Depends on: T13, T15.
- Acceptance: another authorized reader can understand and reproduce the selected
  workflow within disclosed limits; all in-scope failed/excluded work is accounted
  for; export does not silently release restricted bytes or claim scientific truth.

## Phase 4 — Remote acceptance and operational recovery

### T18 — Prove laptop-independent execution on the selected deployment

- [ ] Run a bounded, separately enabled real acceptance fixture and retain the
  deployment identity, timestamps, operation/provider IDs, artifacts and results.
- Depends on: T06, T08–T16.
- Required cases:
  - Laptop physically off/disconnected beyond normal heartbeat and lease windows;
    remote work progresses and the same operation returns on reconnect.
  - Human decision needed during absence; dependent work waits durably.
  - Lost submit reply; lookup finds the original job, with no replacement submit.
  - Mid-run cluster outage and simultaneous control outage; supervisor retains
    ownership, expiry enforcement and honest uncertainty.
  - Grant/credential expiry and worker restart; no extended authority or reset
    deadline, and no invented checkpoint/resume semantics.
  - Stop during execution/outage; requested and confirmed states stay separate.
  - Storage exhaustion/interrupted output transfer; evidence is retained or the
    failure is reported without a false complete result.
  - Successful, failed, cancelled and inconclusive outcomes all appear in review.
- Acceptance: this evidence supports the precise advertised laptop-off behavior.
  Local multi-process simulation and external-runner attestations alone do not.

### T19 — Document and exercise active-backend recovery

- [ ] Distinguish restart of the original authority from backup restore. Define
  the selected deployment's response to loss of the active backend, including
  old-job reconciliation, credential fencing and retained metadata/object custody.
  Add operational visibility for stale workers, orphan jobs, decision age,
  unresolved spend, artifact failures and backup health.
- Depends on: T08, T10, T11, T13.
- Acceptance: demonstrate the supported recovery procedure. Current held restore
  remains read-only; do not clear the hold or clone active grants as a shortcut.
  If active failover remains unsupported, document the manual recovery boundary
  and demonstrate that no duplicate execution authority is created.

## Phase 5 — Validate value before expanding

### T20 — Measure the pilot and decide the next scope

- [ ] Compare the complete workflow with the researcher's existing process:
  setup, intervention, review, verification, recovery, quality, cost and repeat
  use without coaching. Record successes and inconclusive/negative outcomes.
- Depends on: T15–T19.
- Acceptance: record a continue/narrow/stop decision backed by measurements. The
  design's proposed 25% effort reduction remains a hypothesis until measured.

## Deferred until the pilot informs demand

Additional harnesses/providers, broad provisioning, GPUs, teams/hosted service,
large skill catalogs, schedules, automatic memory, general agent fan-out,
publication connectors and database/workflow-engine replacement are not required
merely to complete this backlog. Pull one forward only when the selected workflow
requires it and the change has an explicit contract and acceptance criterion.

## Review traceability

| Review finding | Implementation tasks |
|---|---|
| F1 — Kubernetes hard-expiry overrun | T01, T18 |
| F2 — Stale registry heartbeat | T03, T18 |
| F3 — Escaped mid-loop observation error | T02, T18 |
| F4 — Schema-43 integration failures | T04, T06 |
| F5 — No ordinary coordinator consumer | T09, T15 |
| F6 — Incomplete remote deployment/runtime | T07–T13, T18 |
| F7 — Missing remote-workflow desktop projection | T14 |
| F8 — Capability/schema/backup documentation drift | T05 |
| F9 — No deployment/setup or user-journey acceptance walkthrough | T07–T18 |

## Astra checkpoint — deferred follow-up TODOs

The Astra additions in the current dirty tree are provider-disabled or synthetic
seams, not a remote deployment or an execution claim. The following statuses make
the remaining review work explicit. “Addressed” means only that the named local
repair has focused fake-client evidence; it is not remote, provider, Kubernetes,
or scientific acceptance.

- [x] **P0 — deployment-profile conformance slice:** Compose explicitly selects
  the `dev` image target while the non-root `lab` target remains opt-in. The
  single-active Kubernetes lab profile pairs namespace default-deny with
  labelled-client ingress and egress on TCP/8765, and its health probes use the
  configured trusted host. The opt-in restart fixture renders the shipped profile
  into its new namespace where feasible, then checks durable backend identity,
  idempotent project-command response, the one durable `project.created` event
  cursor, and label-plus-UID-bounded cleanup. This is local/synthetic harness
  coverage only; it does not establish NetworkPolicy enforcement, live Kubernetes
  success, PostgreSQL, HA, scheduling, or provider behavior.
- Focused evidence (2026-09-16, dirty working tree): `.venv/bin/pytest -q
  tests/test_kubernetes_lab_deployment.py
  tests/test_kubernetes_control_plane_restart_runner.py` passed 19 tests. The
  opt-in Docker Desktop fixture was not run, so this is not a Kubernetes
  conformance result.

- [ ] **F1 — hard-expiry overrun:** status: addressed locally by T01, including
  the wall-clock rollback repair. Re-record the scoped dispatch/execution evidence
  at the next checkpoint and prove the selected target's expiry behavior only in
  the separately authorized T18 fixture.
- [ ] **F2 — stale registry heartbeat:** status: addressed locally by T03.
  Exercise remote worker identity, heartbeat custody, and outage behavior in T10
  and T18; do not infer those properties from the fake worker.
- [ ] **F3 — escaped observation error:** status: addressed locally by T02.
  Validate the selected cluster's read-error/reconciliation path in T18 without
  creating a replacement operation.
- [ ] **F4 — schema-43 integration failures:** status: addressed locally by T04
  and the baseline gate in T06. Re-run migration/recovery gates on the selected
  deployment packaging before it becomes an operational profile.
- [ ] **F5 — ordinary coordinator consumer:** status: deferred. Implement the
  independently hosted, scoped, durable Astra consumer in T09, including health,
  bounded retry, restart recovery, and an honest unavailable state. The current
  provider-disabled facade and fixture completions are not that consumer.
- [ ] **F6 — remote deployment/runtime:** status: deferred. Select and document
  the always-on control service, worker, credential custody, storage, and recovery
  profile in T07–T13 before any remote claim. No desktop relay, local volume, or
  interactive bearer-login dependency may remain.
- [ ] **F7 — remote-workflow bridge:** status: deferred. Complete and verify the
  typed preload/main/renderer `remoteWorkflows` bridge in T14 against a normal
  backend consumer, including reconnect, freshness, checkpoint, and
  cross-project fencing. Synthetic timeline reads do not establish resume.
- [ ] **F8 — capability/schema/backup documentation drift:** status: locally
  addressed by T05 but remains a documentation TODO for every newly enabled
  profile. Update capability, custody, schema, and backup claims together with
  its evidence; held restore must remain read-only unless a separate contract
  changes it.
- [ ] **F9 — end-to-end admission, execution, and evidence flow:** status:
  deferred. Deliver T12, T13, and T15 as one readable flow from proposal through
  explicit approval and separate execution admission to remote work, retained
  measurements/evidence, and human conclusion. Keep proposal, approval, grant,
  dispatch, custody, and scientific review distinct.

### Required deferred acceptance walkthroughs

- [ ] **Deployment/setup walkthrough:** write and exercise a selected-target
  operator walkthrough for discovery, immutable plan review, explicit apply,
  identity/status verification, bounded rollback/reconciliation, and evidence
  retention. It must state prerequisites, credential custody, safe abort points,
  and that setup neither admits work nor invokes a provider.
- [ ] **User walkthrough 1 — proposal to evidence:** document a researcher using
  the desktop to create the bounded proposal, approve scope, perform separate
  execution admission, inspect retained remote evidence/negative outcomes, and
  record a human conclusion without direct database or test-only completion calls.
- [ ] **User walkthrough 2 — reconnect and stop:** document a researcher closing
  the desktop or losing connection, reopening the same operation/history, making
  a required decision, and distinguishing requested stop from confirmed
  termination and unresolved exposure. T18 must supply the live acceptance
  evidence before this is advertised as laptop-independent behavior.
