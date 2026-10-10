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

## Current convergence checkpoint — evidence is narrower than the objective

The objective above remains **open**. The source-only developer alpha has local
control, research-ledger, artifact, and execution components, not an accepted
Claude Science alternative. In particular:

| Dimension | Implemented / observed boundary | Still required for the objective |
|---|---|---|
| Beyond one laptop | Separate desktop/control/worker processes and explicit remote-routing contracts exist; the local API comparison has fake external effects. The retained 2026-10-03 patched-success checkpoint records an authenticated Docker Desktop Kubernetes Job observed `Succeeded` (one success), with terminal worker control state `exited`; operation `eb416142e82e70c64e25ef58769949d9`, dispatch `kubernetes_dispatch_60c16ab79b934479b6bc3053526001c7`. The 29-byte output is linked to the artifact and measurement records and matching SHA-256 `79e9e44fe379d350f50b6d52d47fb545c32137a775c063a89ba280d788f8b12c`; collection was read-only and UID-preconditioned. Artifact disposition is `quarantined`; review remains pending. This is a single authenticated, provider-disabled local-cluster job, not laptop-independent operation. | Name and deploy an independently hosted control home, coordinator, worker, credentials and durable artifact store; prove accepted work progresses while the laptop hosting the desktop is **off**, across reconnect and failure drills (T07–T19). Docker Desktop on the laptop does not meet this requirement. |
| Diverse environments | Local CPU/Docker and Kubernetes fixture paths, and provider-neutral/remote target contracts, cover different adapter shapes. The remote comparison pilot has offline tests, not a recorded selected-remote-target run. | Prove one selected non-laptop CPU target's effective isolation, identity, output custody, and bounded operation; then test portability against a second distinct environment without inferring universal support from a Docker Desktop fixture. Keep provider activation and billing evidence separate (T10–T13, T18). |
| Evidence and research | The 2026-10-03 Docker Desktop fixture receipt records a baseline and intervention with two succeeded Kubernetes Jobs; distinct retained, non-late, linked outputs and API-derived `sum_squares` values (385 and 2870) match their respective hashes. Both grants were reconciled at zero settlement. Scientific validity is explicitly `not_assessed`; the artifacts are quarantined and human scientific review is pending. | Validate a source/input-bound research question, negative/inconclusive cases, reproducibility limits, and a human-reviewed conclusion in the normal desktop/API flow. Measure researcher effort and review quality against the existing process before claiming differentiated research value (T13–T20). This deterministic CPU comparison is operational and measurement-pipeline evidence, not scientific validation. |

### Dated evidence update — 2026-10-03

The later retained 2026-10-03 two-variant end-to-end checkpoint records
the bounded opt-in `scripts/check_kubernetes_lab_two_variant_e2e.py` run against
Docker Desktop. Its recorded SHA-256 is
`39885c594dc60c241ea0bac3409ae4c4b1ffe2440d698fe1e59cf5b3b35a460f`; namespace
UID `d89660e4-19cf-43c5-bb94-36a5f5d44ba0` contained two succeeded Jobs. The
baseline retained output (36 bytes, SHA-256
`db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`) yielded
API-derived `/sum_squares = 385`; the intervention retained output (37 bytes,
SHA-256 `5b39ce98f31908a1b20f62cf901e1ad6e19eaf025eb6ba79ff9963f5186d1107`)
yielded `2870`. Both outputs are linked to their attempts and measurement records,
marked non-late in the receipt, and quarantined; both grants reconciled at zero
settlement. The receipt records state `two_jobs_succeeded_review_pending` and
`scientific_validity: not_assessed`: no scientific conclusion is claimed.

Keep earlier recovery history distinct: the initial pre-manifest attempt remained
unknown and was reconciled by the owner-attributed no-start path after worker
credential expiry at zero settlement; that does not prove the attempt never ran.
A subsequent baseline attempt succeeded operationally, but its collector upload
was late under the runner bug, so it is not measurement-eligible; its grant was
settled and attempt remains unknown. Neither history is erased or relabeled by the
later successful baseline/intervention pair. A separate patched single-job
success is recorded above. The bounded two-variant run does not establish
laptop-off operation, a second environment, provider-enabled execution, scientific
validity, agent acceptance, production readiness, or completed researcher review.
Those gates remain open.

The earlier [implementation-progress checkpoint](docs/implementation-progress.md#docker-desktop-authenticated-dispatch-fixture-hardening-2026-09-28)
records a blocked Docker socket **at that checkpoint**, not a permanent
prohibition or a pass for the later reported operation. Record the actual run's
redacted command/receipt, target and namespace UIDs, terminal observation,
output/custody state, and fresh namespace-absence observation in the evidence
tracker before treating even the selected-target fixture as accepted. Do not
promote any of these local or reported results to provider-enabled, remote
laptop-independent, or scientific acceptance. The work packages below and
T07–T20 remain unchecked until their own evidence gates are satisfied.

## Milestone — Single-active lab operationally complete and useful investigation

This milestone turns the existing provider-disabled, single-active lab profile
into one useful, bounded investigation path: source-bound baseline and claim,
human-approved execution, retained outputs, and a human conclusion. Work packages
are ordered dependencies, not parallel completion claims. The current profile and
its synthetic seams remain development evidence until the indicated acceptance
evidence is recorded.

### Claim boundary for this milestone

- **Developer claim:** temporary-state/fake-effect tests and opt-in local fixtures
  establish only the named component behavior. They do not establish a deployed
  control plane, cluster isolation, unattended execution, source validity, or a
  scientific result.
- **Single-active lab claim:** one explicitly named control-plane home, one active
  writer, one identified researcher and selected target can complete the accepted
  investigation while the desktop reconnects. It requires the package-specific
  operational evidence below. It does not mean HA, failover, multi-user sharing,
  or provider/model activation.
- **Shared-organization claim:** requires separate organization identity, roles,
  concurrent-writer, collaboration, and recovery contracts. It is out of this
  milestone even where tenant-scoped records already exist.
- **Provider-enabled claim:** requires a separately authorized pinned provider,
  credential-delivery, billing/usage, cancellation, and live evidence gate. A
  deterministic or provider-disabled harness is not provider-enabled execution.

### M1.1 — First deliverable: Recovery closure for worker identity loss [P0]

This is the first implementation item, not a roadmap placeholder. It closes the
case where the worker credential/principal or registered worker disappears after
an external operation has been admitted, so the original identity cannot report
or cancel it.

- **Scope and proposed state/API model:** add a durable, tenant-scoped recovery
  record keyed by immutable `(grant_id, lease_id, epoch, operation_id)` and the
  original `worker_principal_id`. Proposed states are `identity_lost` (old
  credential is revoked, registry row retired, or identity cannot authenticate),
  `reconciliation_required`, `cancel_requested`, `independently_observed`,
  `terminal_confirmed`, and `unresolved`; only the last two close recovery.
  `POST /execution-grants/{grant_id}/recovery-reconciliations` is an
  owner/operator-authenticated, idempotent command with expected grant/recovery
  revisions and an explicit stable operation identifier. It may request
  cancellation and record a separately authenticated observer's bounded
  lookup/cancellation receipt, but must never issue a new lease, bind a replacement
  worker, replay submit, or turn an unobserved cancellation request into a terminal
  result. Read endpoints expose the recovery state and evidence references without
  credentials. Put state, event, and idempotency writes in one transaction.
- **Likely files/components:** `src/tessera_control/execution.py`,
  `execution_models.py`, `execution_api.py`, `external_operations.py`,
  `remote_workflows.py`, `workers.py`, `migrations.py`, and, only if needed for
  an independent observer protocol, `src/tessera_worker/kubernetes_execution.py`,
  `kubernetes_provider.py`, and `kubernetes_contracts.py`.
- **Default tests:** add focused temporary-SQLite/API tests beside
  `tests/test_workers.py`, `tests/test_external_operations.py`,
  `tests/test_remote_workflows.py`, `tests/test_kubernetes_dispatch_recovery.py`,
  and migration/recovery tests. Cover revoked/retired/lost old identity, duplicate
  recovery command replay, wrong tenant or stale revision, stable lookup after a
  lost reply, independent observer disagreement, cancellation requested versus
  terminal confirmation, control restart, and no replacement submit/lease.
- **Opt-in live tests:** extend a separately enabled fixture under
  `scripts/` and its runner test to remove or revoke the fixture worker identity
  after exactly one labeled Kubernetes Job exists, then use a separately enrolled
  observer to lookup and cancel that exact Job. It must use an explicit kubeconfig,
  context, new fixture namespace, bounded timeout, and retained non-secret receipt;
  it is not a provider test.
- **Acceptance:** the old identity is fenced immediately; a replacement cannot
  acquire authority for the old operation; restart preserves one recovery record;
  independent reconciliation either supplies an attributable terminal observation
  for the original operation or leaves durable `unresolved` exposure visible; and
  all focused default tests pass. A live pass may support only the single-active
  lab recovery claim for its selected target.
- **Non-goals:** active-backend failover, automatic worker replacement, credential
  issuance, shared writable takeover, universal provider cancellation, and treating
  a deleted registry row as proof that the workload stopped.
- **Evidence to record:** tested commit/tree and schema version; operation/grant/
  lease/epoch and recovery IDs; old-identity fencing event; observer identity and
  stable lookup/cancellation receipts; terminal or unresolved state; exact test
  command/count; target identity and cleanup/retention outcome for any opt-in run.

### M1.2 — Source-bound evidence and claim baseline [P0; after M1.1]

- **Scope:** make the selected investigation's source, input snapshot, baseline,
  intervention, observations, interpretation, and human claim explicitly linked
  and versioned. Default evidence remains exploratory until a human records review;
  corrections must expose affected claims rather than overwrite history.
- **Likely files/components:** `src/tessera_control/research.py`,
  `research_models.py`, `research_api.py`, `experiments.py`, `experiments_models.py`,
  `result_acceptances.py`, `artifact_manifests.py`; `desktop/renderer.mjs`,
  `desktop/main.mjs`, `desktop/preload.cjs`; `tests/test_research.py`, new
  `tests/test_experiments.py`, `desktop/test/renderer-state.test.mjs`, and
  `desktop/test/study-export.test.mjs`.
- **Default tests:** temporary-state tests for source digest/version binding,
  baseline-before-intervention fencing, missing/opposing evidence, a negative or
  inconclusive outcome, claim review, correction impact, tenant isolation, and
  export labeling of exploratory versus reviewed material.
- **Opt-in live tests:** a local, fixture-owned source/input snapshot only; verify
  digest readback and correction projection without network retrieval, providers,
  or real research data.
- **Acceptance:** a researcher can inspect one baseline and one intervention with
  source/input/measurement links, record a human conclusion or an explicit
  inconclusive result, and see later corrections mark the dependent claim for
  review. This supports a developer claim only until M1.6 supplies the lab journey.
- **Non-goals:** literature crawling, automated scientific validation, publication,
  team review routing, or a claim that successful execution proves a hypothesis.
- **Evidence to record:** immutable source/input/design digests, linked record IDs,
  reviewer and correction lineage, rendered/exported labels, exact test results,
  and any fixture provenance.

### M1.3 — Verified Kubernetes isolation and safe cancellation [P0; after M1.1]

- **Scope:** verify the selected lab target enforces the declared namespace,
  identity, image, resource, network, output-mount, expiry, and cancellation
  boundaries; retain unknown termination rather than invent a successful stop.
- **Likely files/components:** `deploy/kubernetes/lab-control-plane.yaml`,
  `docs/kubernetes-lab-control-plane.md`,
  `src/tessera_worker/kubernetes_contracts.py`, `kubernetes_provider.py`,
  `kubernetes_dispatch_service.py`, `kubernetes_execution.py`, and
  `docker_desktop_kubernetes.py`; `scripts/check_kubernetes_lab_experiment_e2e.py`,
  `tests/test_kubernetes_contracts.py`, `test_kubernetes_dispatch.py`,
  `test_kubernetes_dispatch_recovery.py`, and `test_kubernetes_lab_experiment_e2e_runner.py`.
- **Default tests:** fake-client conformance tests for explicit target binding,
  no-current-context fallback, deny-by-default manifest intent, labels/UID cleanup
  fence, expiry, lost observation reply, cancellation replay, and requested versus
  confirmed termination.
- **Opt-in live tests:** a new fixture-owned namespace on the named cluster proves
  effective NetworkPolicy denial/allowed control path, non-root/resource limits,
  PVC/output constraints, exact-Job lookup, and cancellation followed by an actual
  terminal observation. Use no provider credentials or production namespaces.
- **Acceptance:** evidence from the selected cluster shows the declared isolation
  controls and one safe cancellation path; failed observation/cancellation remains
  visible as unresolved. This supports only a selected-target, single-active lab
  operational claim, never a general Kubernetes or provider claim.
- **Non-goals:** cluster provisioning, ingress/TLS management, autoscaling, GPU,
  multi-namespace scheduling, or claiming NetworkPolicy enforcement from YAML alone.
- **Evidence to record:** cluster/kubeconfig-reference/context/CA and namespace
  identities, immutable image digest, policy/resource observations, Job/Pod IDs,
  cancellation receipt and terminal observation, fixture limits, and cleanup state.

### M1.4 — Ordinary coordinator and worker consumers [P1; after M1.1–M1.3]

- **Scope:** run durable, independently started Astra coordinator and worker
  consumers against the selected control-plane home, with scoped identities,
  bounded claims/retries, health, restart reconciliation, and an honest unavailable
  state. Preserve deterministic/provider-disabled mode as a distinct profile.
- **Likely files/components:** `src/tessera_control/astra.py`, `astra_api.py`,
  `astra_status.py`, `astra_status_api.py`, `src/tessera_agent/coordinator.py`,
  `src/tessera_worker/service.py`, `control_channel.py`, `cli.py`, deployment
  manifests/runbooks, and `tests/test_astra_coordinator.py`,
  `test_astra_status.py`, `test_worker_channel.py`, `test_development_worker_service.py`.
- **Default tests:** queue claim fencing, duplicate/retry behavior, consumer crash
  and restart, worker reconnect before further effects, unavailable projection,
  tenant isolation, and Electron exit independence using fake effects.
- **Opt-in live tests:** start the two ordinary consumers against the fixture lab
  control plane, restart each once, and record that one already-admitted synthetic
  operation is reconciled rather than duplicated; no model/provider call.
- **Acceptance:** a normal API/desktop request reaches a running consumer without
  fixture-seeded completion; restart neither loses nor duplicates a turn/operation;
  absence is visible and fail-closed. This is a single-active lab consumer claim,
  not a provider-enabled agent claim.
- **Non-goals:** autonomous planning authority, broad fan-out, provider activation,
  HA queues, hosted service, or a second user.
- **Evidence to record:** deployment/consumer identities and versions, turn and
  operation IDs, claim/retry/restart events, health/unavailable projection, exact
  test results, and any opt-in fixture record.

### M1.5 — Complete output custody, retrieval, and reproduction bundle [P1; after M1.2–M1.4]

- **Scope:** collect declared outputs before workspace reclaim, quarantine and
  verify bytes, commit durable project custody, offer authorized retrieval, and
  produce a bounded reproduction bundle containing inputs, manifests, environment/
  code references, measurements, decisions, and explicit missing/restricted items.
- **Likely files/components:** `src/tessera_worker/outputs.py`, `artifacts.py`,
  `sealed_outputs.py`, `src/tessera_control/artifact_api.py`,
  `artifact_manifests.py`, `project_artifacts.py`, `project_artifact_retention.py`,
  `persistent_output_storage.py`, `snapshots.py`, `recovery.py`,
  `desktop/study-export.mjs`, `tests/test_worker_outputs.py`,
  `test_artifact_upload_failures.py`, `test_project_artifacts.py`, and
  `test_recovery_artifact_bundle.py`.
- **Default tests:** declared-output-only collection, byte/inode exhaustion,
  unsafe paths, interrupted upload, checksum mismatch, restart/reconciliation,
  retrieval authorization, held-restore read-only behavior, restricted-byte export,
  and complete/incomplete reproduction-bundle labeling.
- **Opt-in live tests:** run the selected target's bounded output quota/restart and
  interrupted-transfer fixture, then retrieve a fixture-owned object and validate
  its manifest/bundle digest. Keep it outside pytest/CI and provider-disabled.
- **Acceptance:** no incomplete, corrupt, unsafe, or unplaced output is presented
  as complete custody; authorized retrieval reproduces the declared bundle within
  its recorded limits; held restore remains read-only. This supports a selected
  single-active lab custody claim, not durable multi-region backup or publication.
- **Non-goals:** unlimited storage, active failover, public URLs, automatic release,
  retention deletion beyond the existing contract, or scientific reproducibility
  beyond disclosed environment/data limits.
- **Evidence to record:** manifest/object/bundle digests and sizes, custody and
  retention states, retrieval authorization/result, transfer/restart observations,
  excluded/restricted material, exact tests, and target storage evidence.

### M1.6 — Researcher desktop journey [P1; after M1.2, M1.4, M1.5]

- **Scope:** deliver one readable desktop path from connection and goal through
  source-bound baseline/intervention, explicit approval and separate execution
  admission, remote status/reconnect, retained evidence, stop state, and human
  conclusion/reproduction retrieval.
- **Likely files/components:** `desktop/main.mjs`, `preload.cjs`, `connection.mjs`,
  `renderer.mjs`, `study-export.mjs`, `desktop/test/renderer-state.test.mjs`,
  `desktop/test/connection.test.mjs`, `desktop/test/ipc-policy.test.mjs`, and the
  existing research/execution/remote-workflow API projections.
- **Default tests:** renderer and IPC tests for connection/project fencing, stale
  revision rejection, disconnect/reopen of the same operation, requested versus
  confirmed stop, negative/inconclusive visibility, source/custody links, disabled
  controls on held history, and no privileged generic IPC.
- **Opt-in live tests:** the native graphical fixture drives the accepted synthetic
  investigation against the selected lab profile, closes/reopens Electron, and
  saves a fixture-owned reproduction bundle; it performs no provider call.
- **Acceptance:** a researcher completes the stated path without direct database
  setup or test-only completion routes and can distinguish proposal, approval,
  admission, dispatch, custody, and conclusion. This is a single-active lab user
  journey only after its opt-in evidence is recorded.
- **Non-goals:** offline approval, live steering where unsupported, mobile/web UI,
  collaboration UI, or implying laptop-independent execution before separately
  recorded selected-target operational acceptance.
- **Evidence to record:** fixture goal/design/operation IDs, screenshots or bounded
  UI trace, reconnect and stop records, exported-bundle digest, exact test results,
  and explicitly labeled synthetic/provider-disabled status.

### M1.7 — Operator observability, backups, and manual recovery [P1; after M1.1–M1.6]

- **Scope:** document and expose actionable single-active status for stale/lost
  workers, unresolved operations/cancellation/spend, decision age, artifact custody
  failures, backup health, recovery holds, and the manual recovery procedure.
- **Likely files/components:** `src/tessera_control/recovery.py`, `cli.py`, `api.py`,
  `workers.py`, `external_operations.py`, `project_artifact_retention.py`,
  `astra_status.py`, `docs/recovery.md`, `docs/kubernetes-lab-control-plane.md`,
  `docs/support.md`, and recovery/CLI/status tests.
- **Default tests:** backup integrity/limits, held restore write fencing, status
  projection authorization, stale-worker and unresolved-operation alerts, missing
  artifact/failed backup indicators, and a manual procedure that cannot mint a
  new lease or duplicate execution.
- **Opt-in live tests:** back up the fixture lab state, restart the single active
  control Pod, perform the documented read-only held restore, and independently
  reconcile any fixture operation. Retain artifacts/receipts; do not simulate HA.
- **Acceptance:** an operator can identify the active backend incarnation and every
  unresolved recovery/custody condition, execute the documented manual boundary,
  and prove no duplicate authority is created. This supports manual single-active
  recovery, not automatic failover or shared-organization operations.
- **Non-goals:** HA/failover, restore-to-active, automatic remediation, paging/SRE
  service, organization administration, or claiming backups stop provider jobs.
- **Evidence to record:** backend/backup/restore identities and digests, status
  snapshots, procedure timestamps and commands, recovery records, retained
  limitations, exact tests, and target fixture observations.

### M1.8 — Optional provider activation and enterprise collaboration gates [P2; after M1.1–M1.7]

- **Scope:** keep provider activation and shared-organization collaboration as
  explicit post-milestone gates. For a chosen provider, define pinned adapter,
  credential delivery, owner binding, budget/usage/accounting, cancellation and
  live fixture. For an organization, define identity/roles, invitations, review
  ownership, concurrent writers, audit, and recovery/fencing contracts.
- **Likely files/components:** `src/tessera_worker/opencode_*.py`, `harness.py`,
  `provider.py`, `opencode_live_adapter.py`, `opencode_compatibility_fixture.py`,
  `src/tessera_control/credential_leases.py`, `credential_lease_api.py`,
  `credential_lease_models.py`, `execution.py`, `execution_api.py`, `astra.py`,
  and new `tests/test_provider_activation.py`, `tests/test_organization_collaboration.py`,
  `scripts/check_provider_activation.py`, and `scripts/check_organization_collaboration.py`.
- **Default tests:** adapter contract, absent/expired/revoked credential,
  owner/tenant/role fencing, usage unknown, cancellation uncertainty, invitation/
  reviewer separation, concurrent-write conflict, and audit retention with fake
  providers/identities.
- **Opt-in live tests:** separately authorized, bounded one-provider call and
  cancellation/accounting reconciliation using existing runtime credentials; and a
  separate non-provider two-user organization fixture. Neither runs in default CI.
- **Acceptance:** no provider-enabled claim until the selected live provider
  fixture records pinned identity, credential custody, bounded spend, cancellation
  and attributable usage; no shared-organization claim until role/concurrency/
  recovery fixtures pass. Until then the milestone remains provider-disabled and
  single-active.
- **Non-goals:** fallback billing, pooled personal accounts, enterprise HA/SLA,
  federation, broad provider catalog, autonomous budgets, or silently promoting
  developer fixtures to customer claims.
- **Evidence to record:** explicit authorization and spending bounds, adapter/model
  and target versions, redacted credential-reference provenance, operation/provider
  IDs, usage/cancellation receipts, role/audit records, exact fixture commands, and
  failures or unresolved exposure.

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
- [ ] **F5 — ordinary coordinator consumer:** status: implemented as a deterministic,
  provider-disabled consumer with authenticated turn claiming/completion and an
  opt-in deployment example. This is not real model inference or proof of an
  independently deployed service. T09 still requires the independent deployment,
  health, bounded retry, restart recovery and unavailable-state acceptance.
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

## Cross-cutting governed remote-agent platform — 2026-10-04

This additive workstream reconciles the current six-principle assignment with
the existing backlog above. It supplements, and does not close or rewrite, M1.1–
M1.8, T01–T20, or the Astra checkpoint. Existing dated operational receipts
remain evidence for only their stated provider-disabled local Docker Desktop
boundary; they are not remote reasoning, independent-host, A2A, or scientific
acceptance. Track ongoing evidence and lane interfaces in
[`docs/remote-agent-implementation-journal.md`](docs/remote-agent-implementation-journal.md).

| Principle / intent check | Launched lane and owned boundary | Required evidence / current state |
|---|---|---|
| Human-issued bounded authority (`authority`) | Authority backend: existing schema/store/models/API/experiments/execution/external-agents/pyproject plus new `agent_authority` | Typed request/consume/revoke/delegate and execution admission; tenant, idempotency, budget, revision, expiry and revocation fences; negative tests. `docs/agent-authority-interface.md` is the frozen interface, not implementation evidence. |
| Durable notebook provenance (`notebook`) | New `agent_provenance` module; integrate authorized notebook/export projections | Correlated, redacted agent/model/tool/permission/message/delegation/experiment outcomes, including failure, denial and unknown; completeness semantics and targeted tests. `docs/agent-provenance-interface.md` records interface decisions. |
| Supervised real reasoning (`remote-runtime`) | New `remote_reasoning_runtime` and `remote_reasoning_store` | Installed supervised process, durable resume and stop/expiry/usage fences. Real reasoning is live acceptance; deterministic/provider-disabled coordinator paths remain distinct. |
| Full governed MCP (`full-mcp`) | New `mcp_server` and `mcp_workflow` | Supported typed workflow across propose/request authority/consume grant/run/status/cancel/observe/review request/export; separately authenticated human decisions and real-client reconnect; no human-token proxy. |
| Interoperable A2A (`a2a`) | New `a2a_peer` and `a2a_store` | Two independent real protocol peers, durable tasks/messages/results and bounded delegation; replay, cancellation and denial tests; document compatibility. |
| Worker/operator robustness (`robustness`) | Worker withdrawn-context discovery, namespace Pod pagination; desktop connection-bound atomic artifact save | Focused regression tests and installed/operator workflows; preserve owner/context fencing and atomicity. |

Cross-lane integrated acceptance (`integrated-journey`) additionally requires a
planner using MCP and critic using A2A, human bounded approval, ordinary remote
experiment/output collection, denied expansion, restart/expiry/revocation and
joined notebook export. **Status: Implemented; bounded schema-52 local installed
journey passed, but root integrated acceptance is not accepted/closed.** The prior
run-5 late/quarantined/cancelled output remains historical; a separate fresh
normal run recorded on-time output and supported `succeeded` resolution. Ordinary
worker renewal is already implemented. The known provenance/A2A code gaps are
closed, but revision-2 follow-up identified the still-missing portable ordinary
Docker MCP+A2A profile; current gate receipt and external acceptance are pending.
See the dated journal below. Local Docker Desktop
is local evidence; the garage host is now identified and authorized, but
independent-host/laptop-off proof still requires safe host inventory/deployment and
secure runtime credentials. Neither is established yet. No unapproved inference spend is
authorized. Mark checks only at the boundary shown by exact evidence; missing
implementation remains work, not a permanent prohibition.

The [design contract](DESIGN.md#53-governed-agent-identity-reasoning-and-peer-transports)
and [operator install/runbook](docs/remote-agent-platform-install.md) define the
authority, MCP, provenance, reasoning and A2A boundaries. Installed package,
schema-52 migration, scoped human maintenance, a fresh on-time local journey and
independent local A2A peer protocol/outbox evidence now exist; see dated receipts
below. These partial boundaries
do not prove real reasoning, successful scientific execution, complete joined
notebook provenance, or integrated acceptance. Runbook commands must match actual
package entrypoints and evidence; remaining service/runtime commands are still
acceptance work.

### Scoped local leaf evidence update — 2026-10-04

The provenance module owner reports `src/tessera_control/agent_provenance.py`,
`src/tessera_control/agent_provenance_models.py`, and
`tests/test_agent_provenance.py` implemented; its focused local SQLite test
command `pytest -q tests/test_agent_provenance.py` passed **15 tests** on
synthetic records, with scoped lint/format/diff checks. This is not notebook
integration: schema/bootstrap, transaction-local denial audit, authorized
notebook/export projection and byte accounting remain open; a separate
agent-scoped notebook read is underway.

The worker/desktop robustness owner reports **59 targeted Python tests plus 3
consumer regressions**, **49 desktop boundary tests plus 2 negative tests**, and
scoped Ruff/npm run check/JavaScript syntax/diff checks passed. Owned paths are
`src/tessera_worker/kubernetes_pod_listing.py`,
`src/tessera_worker/kubernetes_remote_service.py`,
`src/tessera_worker/kubernetes_output_collector.py`, their
`tests/test_kubernetes_pod_listing.py` and `tests/test_kubernetes_remote_service.py`
coverage, plus `desktop/artifact-save.mjs`, `desktop/connection.mjs`,
`desktop/main.mjs`, and `desktop/test/artifact-save.test.mjs`. Kubernetes clients
and dialogs were synthetic; desktop file writing used the actual local
filesystem. This is not live Kubernetes or native picker proof. Withdrawn-context
list-poisoning remains pending, so aggregate `robustness` stays open. The
unverified native sequence is credential → Open picker → released artifact
preview → Save picker → verify bytes/hash → reconnect. These are scoped,
self-reported leaf results, not full-suite results or closure of the broader
cross-cutting acceptance checks.

### MCP/runtime/provenance and journey-harness update — 2026-10-04

- **MCP:** `src/tessera_mcp/server.py` / `mcp_workflow.py` now expose 15 typed
  tools; the owner reports 38 synthetic focused tests plus one schema test passed
  and an initial real SDK proposal/context/reconnect/denial run. A later
  standalone rerun stopped before MCP protocol calls when fresh control
  readiness returned **503 `schema_mismatch`** during concurrent schema
  integration. `docs/mcp-full-workflow.md` and the two `scripts/check_mcp_*.py`
  entries record exact coverage. Full grant/research/notebook/export live
  acceptance remains open.
- **Reasoning:** the owner reports 25 synthetic tests and scoped checks for
  `src/tessera_agent/remote_reasoning_runtime.py` and `remote_reasoning_store.py`;
  details/live prerequisites are in `docs/remote-reasoning-runtime.md`. The
  `pyproject.toml` now registers `tessera-reasoning` alongside the documented
  module invocation; a fresh wheel build/install and installed command invocation
  remain unverified. No secure enrolled-agent
  credential, dedicated approved included-OAuth harness profile, or named
  independent host was supplied; no inference was run and the existing coding
  agent service was not reused. Usage-cap and interrupt semantics do not prove a
  hard pre-token spend ceiling or provider termination.
- **Provenance:** the owner reports 27 local SQLite tests, including 12 scoped
  `notebook:read` cases. Projection implementation is present, but schema and
  route/enrollment/action integration, denial audit and authorized notebook/
  export byte accounting remain open.
- **Integrated journey harness:** `dev/remote_agent_platform_live.py`,
  `tests/test_remote_agent_platform_live.py`, and
  `docs/remote-agent-platform-journey.md` are present; the owner reports 19
  isolated safety tests. No services or live journey were started. Fresh MCP
  subprocess/session replay is not a network-fault or service-restart proof.
  Real restart-generation, retained-output bytes, independent A2A peer, and
  joined complete export remain prerequisites.

These are self-reported leaf-level evidence, not full integration or closure of
`notebook`, `full-mcp`, `remote-runtime`, `a2a`, `integrated-journey`, or
`journal-install`. Keep the 503 failure and the missing secure-host references as
open blockers with the concrete next actions in
[`docs/remote-agent-implementation-journal.md`](docs/remote-agent-implementation-journal.md).

### A2A peer and latest operator preflight update — 2026-10-04

- **A2A peer protocol:** owner reports official A2A 1.0.0 JSON-RPC using
  `a2a-sdk[http-server]==1.2.1`, `src/tessera_control/a2a_peer.py` / `a2a_store.py`,
  tests `tests/test_a2a_peer.py` / `tests/test_a2a_store.py`, and opt-in
  `dev/check_a2a_fixture.py` / `dev/check_a2a_peers.py`. A local live fixture
  used two independent real peer processes, two SDK clients and actual control
  HTTP. Discovery, send/get/result, restart/replay, cancel/late-result,
  authentication, cross-peer/stale-epoch denial, ancestor revocation and expiry
  replay denial passed. Reported tests: 10 SDK/store plus a body-limit test;
  defaults 4 passed/1 optional-SDK skip; scoped Ruff passed. Provisioned records
  were synthetic. This is a bounded local peer protocol result, not agent
  reasoning, off-host proof, or complete notebook/MCP research integration.
- `pyproject.toml` and `uv.lock` now include the pinned `[a2a]` extra and
  `tessera-a2a-peer`, but a fresh wheel install/entrypoint check is pending and
  `docs/a2a-peer.md` still has a stale packaging-pending note. The reasoning bridge
  and provenance append/export collector are still being integrated.
- **Runtime:** newer owner report is 26 synthetic tests (not the historical 25
  count above), with Basic authentication to the guarded OpenCode harness and
  bearer authentication only to Tessera control. The inspected OpenCode build is
  `2.0.18-subscription.20260929`; its owner account database is shared and there
  is no dedicated research profile. Isolated local test-agent enrollment can be
  issued through separately authorized human setup; no user secret is needed.
  Actual live blocker is a dedicated approved included-OAuth research profile
  and private references plus an authorized host. No inference was called.
- **Journey preflight:** latest owner report is 31 schema/safety tests and a
  read-only UID-fenced generation baseline probe passed, but no restart occurred.
  Preflight found TLS route expired/no listener, ordinary worker unready with 36
  restarts, and the selected installed wheel lacks the newly added modules.
  Planner references are placeholders, dedicated reasoning references absent,
  and the historical grant is already reconciled. Same owner continues
  read-only diagnosis and setup orchestration; no replacement harness or service
  restart is authorized by this evidence. Integrated user flow remains open.

These self-reported leaf results preserve separate boundaries: real local A2A
protocol exchange passed, while live model reasoning, autonomous critic behavior,
full MCP workflow, complete notebook provenance/export, restart recovery,
independent-host/laptop-off and final `journal-install` remain unaccepted.

### Backend, service profile, and provenance collector convergence — 2026-10-04

- **Authority/research backend:** owner reports 63 targeted tests passed in
  103.07s, including schema-51 fresh initialization and explicit v50-to-v51
  migration with readiness 200. Implemented human-issued authority, transaction-
  local agent consume/delegation fences, typed MCP research workflow, correlated
  provenance (including denials surviving rollback), same-snapshot human export,
  and withdrawn-context discovery omission. The broader combined
  research/experiment/Kubernetes regression commands timed out (120s/300s);
  they are incomplete, not passing evidence.
- **Bounded provenance collector:** `agent_provenance_api.router` and
  `app.state.agent_provenance = AgentProvenanceStore(state_dir)` are registered
  against schema 51. `provenance:append` must be explicitly enrolled, has a
  nine-action maximum, and is not inferred for existing enrollments. Four focused
  app HTTP tests passed in 8.80s: grant-bound append/replay/conflict, notebook/
  export, and scope/destination/project/subject/revocation denials. No schema
  number bump or old-grant widening was made. Live runtime/A2A outbox delivery
  remains unobserved.
- **Runtime services:** `deploy/remote-agent-platform/`,
  `src/tessera_agent/service_startup.py`, and
  `tests/test_remote_agent_services.py` implement Linux systemd templates and a
  fixed `LoadCredential` mapper. Owner reports 57 tests (31 service-profile plus
  26 runtime) passed, with scoped checks. No Linux/systemd host or fresh wheel
  install was available/run, so installed lifecycle remains unverified.
- **Critic bridge (at this earlier checkpoint):** owner reports 25 focused tests for
  `src/tessera_agent/a2a_reasoning_bridge.py`, using exact approved task-profile/
  digest/context fences, durable runtime task linkage and redacted outbox.
  Actual delivery had not yet been observed at that checkpoint. Later local
  delivery implementation/evidence is recorded in the latest bridge/outbox item
  below; ordinary TCP service delivery remains unverified.
- **MCP/live journey (latest bounded result):** the leaf reports 29 real
  SDK/stdio/control calls passed across proposals, separately human-issued
  authority, admit/dispatch/status/cancel, observation/review, dual-cursor
  notebook/export, reconnect replay, denial, revocation and expiry. The final
  standalone control harness passed; 46 targeted tests and scoped checks passed.
  Synthetic package/queue fixtures only; no worker/executor, cluster, experiment,
   inference or paid-provider call. This passes the local protocol/control boundary,
   not ordinary-worker or integrated MCP/A2A researcher acceptance. Later
   installed attempts remain incomplete as documented below.
  Historical 503 `schema_mismatch` remains preserved. The A2A peer result is still
  narrower than an autonomous critic journey. See the install guide and journal.
- **Critic bridge/outbox (latest):** `src/tessera_agent/a2a_reasoning_bridge.py`
  now reports optional terminal `--deliver-provenance` using the shared client,
  with 34 focused tests and local HTTP/SQLite append/replay/receipt/atomic-ack
  coverage. Runtime `src/tessera_agent/provenance_client.py` reports 58 targeted
  tests, durable runtime outbox, and declared/locked optional `httpx`. Collector
  route and SQLite tests used in-process synthetic transport; ordinary TCP service
  delivery and real reasoning are not established. Denied/unknown bridge outcomes
  remain pending rather than acknowledged. Joined local notebook behavior is
  implemented; full authorized export/journey acceptance remains open.

### Broader Python gate and repairs — 2026-10-04

- First reported broad revision `32c4...`: lint/format 562 passed; shard 0 had
  677 passed, 1 failed, 1 skipped (679.7s); shard 2 had 587 passed (797.2s);
  shards 1 and 3 timed out at 900s. The first failures exposed agent status using
  a human-only projection and a quota fixture discrepancy (expected 3, actual 2).
  Agent resource/status exports now withhold notebook data explicitly without
  widening authorization, while human project exports remain unchanged. Fixture
  cleanup was fixed to preserve the expected three Jobs/four Pods. Focused privacy/
  preflight set: 16 passed in 27.75s at source digest
  `4e0319220ac216404ea0d972debb7e7c4c246ad2cb8251e900fef4491f4cedfa`.
- Next attempt: lint/format 563 passed; shard 0 had 678 passed, 1 skipped
  (463.32s); shard 1 had 806 passed, 1 failed (847.72s), with 249 tests unrun;
  shards 2/3 were not started. The failure found migration clearing held-restore
  backup forensic metadata. Repair invalidates backup freshness only for the
  active recovery state; held metadata/history and mutation denial remain intact.
  Focused migration/recovery set: 9 passed in 43.35s at source revision
  `8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9` (404 files,
  5,794,639 bytes). A subsequent frozen sequential gate passed on this exact
  unchanged revision; the receipt and exact shard outcomes are recorded below.
- At that checkpoint, assigned source writers reported quiescent after these
  repairs. Later installed testing exposed queued-dispatch recovery as a distinct
  implementation gap. The frozen source baseline completed, but the later
  recovery revision still needs a fresh full gate. Installed worker/output,
  Linux/service and external-reasoning acceptance remain open.

### Frozen baseline and installed recovery continuation — 2026-10-04

- Frozen local Python source gate on unchanged digest
  `8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9`
  (404 files, 5,794,639 bytes; source edits false): Ruff check passed, format
  covered 564 files. Sequential shards 1/2/3/0 reported **1056/591/1129/678
  passed**, total **3454 passed, 0 failed, 1 skipped**; pytest durations were
  1169.32/556.51/979.35/502.73s. The skip was optional A2A SDK absent in the
  gate environment. Receipt:
  retained frozen-baseline gate checkpoint.
  This is a completed baseline only; it does not cover later recovery changes.
- A fresh installed recovery wheel/image implements the no-package case at source
  digest `2d93d6ecc1fe8134d4aa4cf82e85ab8bbf79dc7f4e180292a5e1a1c95c1d879c`.
  New recovery tests: 13 passed in 53.42s; existing execution lifecycle tests: 14
  passed in 24.89s; 27 targeted installed recovery/lifecycle checks reported.
  Human reconciliation returned 200 and same-key replay matched; state retained
  admission/lease history, recorded `not_executed`, no package/outbox/operation,
  and no provider-termination inference. This does not repair queued-dispatch
  recovery. The full gate must run again on this digest.
- Latest installed journey passed issue/admit/package/dispatch but failed retained
  output because prep omitted `network_none_evidence` from target identity. The
  prep code is corrected; already-issued immutable dispatch digest was not
  rewritten. Human cancellation remains `cancel_requested`; expected Job lookup
  found zero matching Jobs among five historical project Jobs, with no Job UID or
  output digest. The exhausted six-page notebook contained metadata only and
  remains `partial_page` (cursor 8/9275). Recover only through the original worker/control and supported
  provider-absence reconciliation; no SQL/fake termination. Full MCP/A2A
  experiment journey remains incomplete. See sanitized receipts and current
  owner handoff in `docs/remote-agent-platform-journey.md`.

### Desktop integration and ordinary-worker follow-up — 2026-10-04

- Desktop owner reports final `npm test --prefix desktop`: **205 passed**, 0
  failures/skips/cancellations in 3.618s; `npm run check` and scoped diff checks
  passed. Packaged Electron hidden-window smoke reported sandbox and context
  isolation enabled, Node integration disabled, no renderer errors, null status,
  and clean exit 0. The corrected test assertion is
  `desktop/test/journey-bridge.test.mjs`; 41 desktop root/test files were
  unchanged during the gate at SHA-256
  `828be9665891ccad6bc6717f4967347d78377fcf44419d76b3e8cf4460ce2a33`. No
  separate desktop build script exists. The test Node was 22.2.0, below the
  documented 22.12.0 minimum. Native OS picker/connected workflow remains
  unverified; fallback selection is not native acceptance.
- Local `robustness` checkpoint reports all three requested repairs passed their
  focused local evidence: withdrawn-context discovery in backend's 63-test gate,
  Pod pagination's 59 Python tests plus 3 consumer regressions, and atomic
  connection-bound artifact save in the desktop gate. This does not prove live
  Kubernetes or native picker operation. Historical earlier status is preserved;
  the latest local evidence supersedes the prior “withdrawn-context pending”
  checkpoint.
- Journey harness owner reports 43 tests (5.55s) for schema adaptation and
  automated human planner/critic enrollment, optional fresh-target worker setup,
  status-only delegation and peer configuration, including expected self-enroll
  403. No service or Job was mutated. Subsequent read-only checks found the
  existing worker credential expired at 11:54:43Z; GET/status returned 401,
  worker logged `ControlChannelError`, and restart count was 39. The expired
  credential cannot be renewed. Preserve its journal/history; plan a fresh worker
  identity/journal instead. This is distinct from the earlier TLS route
  expired/no-listener and 36-restart preflight. No restart, replacement, dispatch
  or cleanup mutation was performed.

Remaining native-picker acceptance sequence: credential → Open picker → released
artifact preview → Save picker → verify saved bytes/hash → reconnect. Use Node
22.12.0 or newer as required by `desktop/README.md`; no native picker success is
claimed from the packaged hidden-window smoke.

### Installed maintenance and distinct run 5 — 2026-10-04

- Preserve the earlier blocked receipt
  for the 2026-10-04 installed queued-expiry checkpoint:
  original worker credential expired 21:14:03Z, operation-scoped Job LIST returned
  403, then original bearer returned 401 at 21:14:21Z. Worker was stopped and its
  workspace/journals retained; no attestation/release or termination conclusion.
- Separately authorized operator provisioned namespace-UID-scoped Job/Pod
  `get,list` only, then used event-backed five-minute maintenance as the original
  human grant issuer. Install returned 201; proof reconciliation returned 200 with
  identical same-key replay and `reconciled`. Maintenance issued no bearer,
  worker/execute capability, renewal or provider permission; a separate temporary
  service-account token served only approved namespace reads. No expired worker
  bearer was renewed and no provider termination inferred. This supersedes the
  blocked status, not its receipt. See `docs/queued-dispatch-maintenance.md` and
  sanitized run-5 receipt.
- Distinct installed run 5 reports all 16 configured stages passed and a real Job
  with 36-byte output, SHA256
  `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`, readback
  matching public CPU value 385. The lease had already been cancelled, so output
  was late/quarantined and the supported disposition was `cancelled`, not success
  or scientific validation. Final eight-page notebook remains `partial_page`,
  metadata-only, and omits output-manifest correlation. MCP observation/review and
  separate pre-submission cancel/reconnect passed; no running-Job termination
  claim. Critic outbox remains unverified.
- The platform is **implemented with bounded schema-52 local installed success**,
  but root integrated acceptance is not accepted/closed; do not mark all checks
  complete. Ordinary worker renewal is already present. Provenance correlation
  and A2A outbox acknowledgements passed the newer installed join/peer drain.
  At that checkpoint no known provenance/A2A source gap remained; the later
  revision-2 follow-up records the separate missing portable ordinary Docker
  MCP+A2A profile. The normal run-5 timeline still records zero renewals and a late
  manifest; its exact cancellation reason is not recorded. The distinct fresh
  normal success does not erase that historical result or the held run-8
  quota/unknown operation.
- Real reasoning remains unadmitted. Proposed profile is a separate guarded
  process/workspace on the shared owner DB (no OAuth copying or coding-service
  attachment), deny-by-default with only explicit MCP tool whitelist, approved
  model, <=3 steps / <=120s / <=8192 **observed** tokens, and included ChatGPT
  OAuth only (no API-key or paid-credit fallback). Token ceiling is not a hard
  pre-token bound. No profile admission/inference occurred; independent-host proof
  requires a separate user decision. See `docs/remote-reasoning-runtime.md`.

### Ordinary worker lease-renewal diagnosis — 2026-10-04

- Correction: renewal was previously listed as a possible implementation seam.
  Inspection of `KubernetesDispatchWorkerService` and its durable action journal
  found it already renews every 10 seconds through `POST /worker/leases/renew`,
  with stable idempotency keys, exact epoch checks, and immutable hard-expiry
  enforcement. Renewal failure/replay is handled by the ordinary service; no
  harness keeper or new runtime/backend/worker code is needed for that path.
- Focused lifecycle pytest summary: **24 passed, 38 deselected**, two dependency
  deprecation warnings, **115.45s**. The outer shell timed out at 120s after the
  completed pytest summary, so no shell exit code was observed. This is selected
  synthetic cluster/control-clock coverage, not a full suite or installed live Job
  test. Scoped Ruff/format and diagnosis-document whitespace checks passed; no
  runtime/backend source changes were made.
- `docs/ordinary-worker-renewal-diagnosis.md` and the newer schema-52 read-only
  snapshot show zero renewal actions, terminal exit at 21:53:20.499345Z, lease
  expiry at 21:53:32.979713Z, manifest recorded at 21:53:34.220823Z and later human
  cancellation at 21:55:11.242538Z. The manifest was recorded after expiry; later
  cancellation was not the cause of the initial `late` flag. The exact closed
  cancellation reason/actor remains unrecorded. Deployment/workspace timeline
  collection was previously delayed by deployment ownership, not an authorization
  denial. Do not infer a renewal failure or termination. The later fresh normal
  run completed on time using the existing ordinary worker (lease→final pins→
  admit→package→dispatch); expiry/revoke checks were separate attempts.

### Earlier provenance and reference-peer outbox convergence checkpoint — superseded

- Provenance local collector integration reports 14 ordinary-peer records joined
  without replay duplicates; sender/receiver attribution, message/result causation,
  null model and revoked-proof retention (3 pending, 0 ack at frozen epoch) passed.
  Final accepted-delivery assertion still fails: optional null reference keys in
  the shared client differ from collector responses that omit those keys, leaving
  accepted ack count at zero. This source lane is not complete.
- Reference A2A lane reports 35 SDK/store/bridge/outbox tests passed in 8.50s. A
  two-process SDK/collector run passed peer discovery, submit/result/replay,
  changed-key denial and cancellation; causal events survived replay. It stopped
  at the final ack assertion, with reference artifacts `verified:false`; servers
  were stopped and no inference ran. Do not call ordinary outbox delivery accepted
  until the client/collector acknowledgement contract converges.
- These two source changes remain active. The final full Python/install gate is
  pending after convergence. Root quota telemetry at 22:39 UTC reported healthy,
  about 30% remaining; not exhaustion. No paid fallback or unapproved inference.

### Schema-52 installed normal success and current acceptance — 2026-10-05

- The retained 2026-10-04 installed fresh-normal-success checkpoint
  reports schema 52, image `sha256:ff90ef05b7ba771c2ee400b3f475e29eeda48cc84a74fe2b61583eb5a2f2b950`,
  wheel SHA256 `d982601e437a0858af1a3572d9e0cd33dd763e40c387c609cd588babd17fd33f`.
  Fresh owned namespace/project had 3-Job/8-Pod/1Gi quota and concurrency one;
  the old six-Job bench, run-5 cancelled output and run-8 quota-denied/unknown held
  state were not modified. No SQL cleanup or concurrency widening.
- All 16 configured installed journey stages passed. Actual on-time Job/operation/
  attempt/manifest/Job UID were correlated; 36 bytes/385 had the expected SHA and
  readback. Human output proof HTTP 201 and same-key replay passed; control resolved
  `succeeded` HTTP 200. Human disposition remained `retain_quarantined`, custody
  released, scientific validity not assessed. Two installed reference peers each
  acknowledged four metadata events, exact replay/no duplicates; final notebook
  joined output plus eight peer events at sequence 112/event cursor 11983. Historical
  off-platform coverage remains unknown. Receipt still says reasoning, independent
  host/laptop-off and root `integrated_acceptance_verified` false.
- Supporting current-code reports: explicit schema-52 migration **24 tests**;
  provenance **58 tests plus a human-HTTP case**; client normalization **78 focused
  tests**; isolated two-peer collector/readback **14 acknowledgements, zero
  pending, replay without duplicates**, with revoked proof's 3 events still pending
  at its frozen epoch (**32.16s**). Earlier client null-key/final-ack failures above
  remain historical and are superseded by these later outcomes.
 - The schema-52 checkpoint verified output-correlation and reference-peer
   acknowledgement only in that bounded local journey. Revision-3 source now
   implements generated-output registration/consumer/reader/peer transport,
   supervisor capture hook, default factory and guarded-profile wiring. The
   remaining platform gap is truthful guard-origin measurement/admission and live
   installed reasoning, not absence of all consumer/profile source. This still is
   not full acceptance. Current per-check status:

  | Check | Owner/components | Existing local/source evidence | Remaining live/executable acceptance |
  |---|---|---|---|
  | `authority` | Authority backend and runtime-binding/native owner | V7 local evidence includes backend 32, schema migration fixtures 18, API/authority 31 and paired-native 25 distinct cases with composite—not single-run—coverage. Same-grant branded WS handshake/frame, readback/exhaustion denial, last-unit reservation and uncertain/restart fences were exercised. Upstream identities/guard/quota remain synthetic. | Genuine human-issued grant, installed operator flow and production enrollment remain required. No real provider dispatch. |
  | `runtime-admission-contract` | V6 contract plus v7 Python Store/API/verifier and isolated native HTTP/WS owner bridge | Strict v1 default, explicit v2 opt-in, unknown required local checks deny. Actual local component path: producer/private guard and quota predicate, authenticated Unix IPC, HTTPS API, verifier before claim, canonical Store writer fence through child exit, actual SDK HTTP/WS and private native lock/settlement. Separate 79-input cold native source gates passed; no full build/install. | Genuine principal/session/publisher/issuer and owner resources, authenticated financial resolver/revocation/profile age, installed full SDK/config/skill/catalog/toolmap and live/off-host journey remain unverified. No production admission or live inference. |
  | `notebook` | Public capture/provenance, registry, durable phase join | Provenance checkpoint: 102 tests/122.22s/2 warnings/exit0, source SHA `c70e2b91b5cbe31861d49b0f3eb19cec672190a05e7362adc69c258e5d1ebc13`; temporary HTTP/SQLite append/replay/export/disclosure/rollback. Owner/guard fixtures synthetic; adapter non-admitting. | No actual private guard, authenticated public feed, real experiment/output join or live notebook. |
  | `remote-runtime` | Isolated public runtime/SDK publisher and factory | Isolated source SHA `6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`; 52 Bun +4 Python checks and two reproducible isolated builds. Retained exact wheel `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99` has local installed-origin/protocol evidence for those bytes only. Factory 11 tests/5.48s/3 warnings, SHA `97311053d3056ec11c79ffe27aa06a8da96ce5bc19a60dcba453c849303ff3dd`. | No actual approved account/principal, live inference, genuine authority binding, final transport, research session, restart or running-job cancellation. Later test/doc edits supersede this wheel as current candidate. |
  | `full-mcp` | Research factory diagnostic/fail-closed consumer + MCP stdio | Retained exact-wheel protocol gate passed one test/eight stages/29.61s/native exit 0; 248 product files/136 origins verified; actual local MCP stdio exposed 18 tools/1 resource and exercised scoped operations/reconnect/replay. | Not full research discovery/catalog, human approval, experiment/review/export or actual admission. MCP child exit remains unknown; no HTTP MCP claim. New test/doc changes require a final wheel-bound protocol receipt. |
  | `a2a` | Independent A2A process compatibility harness | Same retained-wheel run exercised independent local A2A peers, replay/reconnect/restart/cancel and five collector receipts. Synthetic critic bytes/human fixtures; peer `-15` is owned cleanup. | No generated critic inference, actual approved peer identities, scientific review or off-host/laptop-off exchange; exact protocol receipt must be rebound to final wheel. |
  | `robustness` | Worker/operator + desktop repair lane | Existing focused local regressions remain passed. | Garage-host operations, live external cluster and native picker remain unverified. |
   | `integrated-journey` | Default runtime factory + partial guard producer + MCP/A2A/human workflow; Docker worker | Frozen source gate passed on 4,268-case snapshot. Schema-52 local 16-stage output/eight ACKs remain historical. Docker worker 112-test source checkpoint and profile 4 static/mapper cases remain separate source evidence. | No complete trusted guard producer/dedicated origin admission; effective/final policy, final request hooks, real included-OAuth entitlement, real inference/review/experiment, installed image, active restart/cancel, garage hybrid, independent host and laptop-off remain unverified. |
  | `observability-slice` | Isolated hook/SDK publisher, Tessera guard join and research factory | 21-case channel +11 factory +102 provenance receipts remain separate. Retained wheel checks bind to `60ec3757…`; guard/authority/protocol outputs remain synthetic, never admission. | Genuine principal/owner guard, final provider transport, live session and admission remain open; operator finance and provider entitlement/billing remain separate/unknown. |
  | `release-candidate` | Packaging, lock normalization, pinned observer bundle and exact-source runner | V5 source `3bd2d1a8…`/wheel `fc537c83…` and its 4,503-case/Ruff receipts remain exact historical evidence. Later package `fbd6…`/`60ec…` has separate double-build/install/protocol receipts, not that full-gate proof. V7 bindings and 79-input cold source closure are source checkpoints only. | No current candidate, clean install/protocol rebind or full-source Ruff/format/pytest gate. Candidate-impact requirements remain; historical evidence must not be relabelled. Live admission/research/device/offhost remains separate. |
  | `journal-install` | Canonical journal/backlog + final packaging | This v7 journal batch records completed local binding/source-closure receipts, synthetic-resource boundaries, remaining secure-owner prerequisites and the read-only tooling review. Scoped documentation checks only. | Root owns final doc reconciliation, exact-source freeze, candidate build/install/full gate and remaining live acceptance. Schema 55 is source-migrated; installed product state is separate. |
 - Preserve first current-source gate failure receipt
  for the schema-54 standard gate (4,223 passed,
  6 failed). Repair labels were facade 21, rows 2–4, native DTO 9, derived fixture
  3, guardian bounded-readiness 3 + concurrent 2, README format 0. These overlap;
  do not add them to 4,268. Final frozen gate used six native commands, all exit 0;
  four shards were 907/1,263/663/1,435. It reported 14 warnings in four categories,
  wall 2,006.651 seconds; Ruff 0.16.6 lint+format exit 0 (677 already formatted).
   The selected snapshot was 713 files/8,997,900 bytes and aligned before/after.
   V4 then changed three Tessera-owned contract files with a separate 37-case
   focused receipt; no 4,268 aggregate rerun followed. Journal/backlog edits after
   that source change were not retested. It is not the historical 3,454/1 baseline.
- No linked gate receipt path was available with the earlier 171-case digest;
  current full-gate receipt is the frozen convergence directory above. Do not infer
  installed distribution, live guard source, admission, inference or host acceptance
  from the source-gate pass.
- Revision-4 first-slice changes: the Tessera-owned phase evidence contract
  (`src/tessera_mcp/guard_phase_evidence.py`, its test and doc) was integrated with
  the isolated OpenCode executor; existing strict complete-attestation admission
  remains unchanged. Root approved the ignored workspace-relative
  isolated observability worktree for the same worker, resolving the
  previous task-dispatch input-validation/workspace-binding issue without a policy
  override, session move, alias or active-control change.
- The sparse selected public OpenCode 2.0.18 snapshot had five byte-pinned baseline
  files and no Git metadata; no upstream commit/release checksum is claimed. Core
  changed `packages/core/src/plugin/hooks.ts` and
  `packages/core/src/session/model-request.ts`; it added
  `packages/core/src/plugin/model-phase.ts` and
  `packages/core/test/model-phase.test.ts`. The actual ordered Effect-context
  executor mints a one-shot post-model-request-hook lease/30-second receipt bound
  to opaque challenge refs and a safe provider/model/tool-schema projection. It
  rejects order registration/disposal drift, safe-field mutation, replay, stale and
  future observations; unsafe prompts/headers/account/body/options/descriptions and
  required-field getters are not collected or invoked.
- Owner-reported core gate: Bun focused test 18 pass/0 fail/38 expectations/321ms,
  exit0; Prettier 3.6.2 on four TypeScript files and isolated Bun syntax build of
  two changed entries passed. These are not full package typecheck, service
  preparation, complete model-request dispatch, final transport or live-provider
  proof. Tessera changed only the existing phase-evidence module/test/doc; combined
  actual executor-to-Python integration reports 57 pytest passes plus Ruff/diff
  exit0 and rejects late ordering before serialization. Updated integrated-receipt
  SHA-256 `8258be7009e79c20941f5f9489c524e4f49dd62cf5befc6e04ca09475f18c2a1`.
  These reported checks were not rerun for this ledger update.
- Reproducible six-code-file artifact revision SHA-256:
  `e0db47b733dde1d5cc2bf60853a949fb9d635a761096a79aed8fc55f319f7b9e`; ordered
  files are the four TypeScript paths above followed by
  `src/tessera_mcp/guard_phase_evidence.py` and
  `tests/test_guard_phase_evidence.py`. Digest construction hashes each UTF-8
  workspace-relative path, NUL, exact bytes, NUL in sequence. Docs, baseline,
  receipts, caches, dependency links and auth/runtime state are excluded.
- Receipt remains intentionally non-admitting: `complete:false`,
  `phase_finality:unproven`, `ordering_enforced:false`. Unsigned JSON alone proves
  neither authentication nor consumption over the installed channel; parser tests
  use a required independent trusted context, not an installed channel. Operator
  financial settings remain `not_supplied`/freshness unknown; provider entitlement/
  billing and guard/quota facts are not fabricated. No full 4,268 gate rerun; its
  frozen digest predates the v4 files, and schema 52 remains last installed evidence.
- The five-gap map separates source/local measured claims, not-run hooks, operator
  financial attestation and provider-unknown. Partial authenticated RPC reports only
  available transformed tools/session-point catalog/loaded skill-body hash/agent
  permissions/provider metadata/session version/active connection/transport origins;
  its 22 source-fixture cases still yield `complete:false`, not admission. The
  separate 17-case owned-child identity library also yields `complete:false` and
  does not prove an OpenCode process, installed Tessera build or child SDK linkage.
  Final hook order/post-hook seal, final effective config, selected raw skill bytes/
  activation, supervised OpenCode build/SDK, and public included-OAuth
  entitlement/billing/no-fallback remain unmeasured. The source gap map is linked in
  `plugins/tessera-harness-observer/README.md`; signatures authenticate origin, not
  measurement truth.
 - Next implementation slice: expose receipt through public SDK/channel, test actual
  service preparation and dispatch consumption and bind final transport; separately
  establish trusted measured guard/quota and operator-financial joins, then measure
  final policy/config/catalog/skill/build/SDK/provider claims at the actual runtime
  boundaries. No admission lowering, synthetic success fills, live inference,
  service replacement/restart, garage/deploy operation, credential access,
  environment-file write, permission/financial-control change or commit occurred.
- Revision 5 supersedes that “next slice” status for the public SDK channel: existing
  revision-5 worker evidence reports 21 focused tests pass (0.63s, exit0) for public
  SDK prepare/read/consume and actual hook layers, with explicit fake upstream/
  authority and zero network; late-order consumption rejects. The sanitized Python
  observer join checks challenge/order/origin/model/fingerprint/time/quota/financial/
  privacy but remains always non-admitting. This is local source execution, not
  genuine owner/guard facts or installed/live admission. Do not repeat the v4 receipt
  to imply full transport or complete observability.
- Revision-5 canonical acceptance matrix and RC boundary are in
  [`docs/remote-agent-implementation-journal.md`](docs/remote-agent-implementation-journal.md).
  RC means versioned reproducible Tessera source/wheel, pinned isolated observer
  source/patch plus baseline identity, deterministic build inputs, clean-install
  entry point/skills/docs checks, compatibility manifest, secret/runtime-state
  exclusion checks, operator runbook and exact-artifact gate receipts. Keep the
  redistributable candidate separate from `.tessera` receipts/worktrees, caches,
  runtime databases, credentials and private guard state. Candidate build is not
  release acceptance, production, deployment, commit or publication.
- Latest accepted owner receipts and exact file/hash boundaries are detailed in the
  [canonical revision-5 journal](docs/remote-agent-implementation-journal.md).
  The staging-only self-version fix changed exactly `dev/build_release_candidate.py`
  and `tests/test_release_candidate.py`; root `pyproject.toml`/`uv.lock` remained
  unchanged. Its 17 focused tests and scoped lint/format/whitespace passed. The
  initial archive's self-version mismatch is historical/superseded. Corrected source
  archive `fbd6a29cc5346f50701c5df92855ec36376f9b1f7c3203df16222305233adc24` and
  wheel `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99` were
  built twice byte-identically with locked sync/install, CLI/origin/resource and
  exclusions passing. See `release-candidate-v5/FINAL-HANDOFF.md`.
- Installed protocol on that exact wheel passed one test/eight stages/29.61s/native
  exit 0; retained v5 final-installed protocol receipt
  (receipt SHA `b908a13ce254f164ef0250f67a1f8f1a3c92707104e5d593bf8ba5f5aa1cb2b0`).
  248 product files and 136 import origins checked; real local MCP/A2A processes but
  synthetic identities/critic output. Installer archive hash remains unknown/null.
- Earlier full pytest attempt 4,444 passed/6 failed/26 skipped/3 warnings is
  superseded after source drift; raw six error bodies and the 26 skip reasons were
  discarded/unknown. Do not infer expected skips or claim this as final acceptance.
  Owner diagnostics found three packaging cases pass with native setuptools 81.0.0,
  no production packager fix required; the protocol `.pth` case now uses a valid
  relative third-party fixture (production guard unchanged; 33 guard cases pass);
  the split-context fixture preserves mandatory policy/integrity requirements while
  omitting absent optional `guard_phase_binding` rather than fabricating it. The
  complete pinned gate-driver handoff is
  retained in the v5 complete-gate-driver checkpoint; all six
  recovered cases passed together, 6/0/0 skipped/3 warnings/exit 0. This is focused
  regression recovery, not the full Ruff/format/repository pytest gate.
- Subsequent test/doc changes revoked that source freeze. Retained exact package and
  protocol receipts apply only to the hashes above; they are not the repaired current
  candidate. Root must capture a new source inventory, rebuild twice, reinstall,
  rebind installed protocol and run the full exact-current-source gate. Earlier
  current-source full-gate attempts remain superseded; no current full gate has passed.
  This is the planned last source-journal batch; record final gate outcomes in receipts
  and task evidence without further source-doc churn. The 4,268 pass remains only the
  frozen pre-v4 snapshot.
- External facts remain distinct: local guard/authority fixtures are synthetic;
  application-provider entitlement/billing/final route are unknown; operator finance
  is `not_supplied` with freshness unknown. Root review of observer patch/baseline/
  license/redistribution is not recorded as approved. The prior quota-based admission
  hold cleared through normal fresh admission for the same owners; coding-agent
  telemetry is 94% remaining at 01:37 UTC 2026-10-07, unrelated to application
  entitlement. No paid fallback, top-up, policy override or owner replacement.
- Packaging and protocol owners report their assigned work quiescent after receipts.
  Root must confirm all remaining writers quiescent and explicitly freeze the final
  corrected source before exact archive builds/full gates. No current-candidate full
  gate is claimed here.
  - Remaining acceptance: the 4,268 full-source gate passed only on its frozen pre-v4
    snapshot. The later 4,444/6 failed/26 skipped candidate attempt was source-drift
    invalidated; its six raw errors and skip reasons are unknown. Six repaired cases
    now pass together in the complete pinned driver, but that is not the repository
    full gate. Retained fbd6…/60ec… source/wheel and installed protocol receipts are
    exact-artifact local evidence only; later fixture and journal edits supersede them
    as current candidate. Root still needs new final freeze/build/install/protocol
    binding and full current-source gate; schema 52 remains installed product state.
    Revision 5 authorizes bounded public
   SDK/harness source changes, not trusted live guard/owner measurements. A trusted
   independently measured guard handoff and actual runtime admission remain
   prerequisites. The separate deployment child safety refusal remains unresolved
  and is not to be retried unchanged. The garage target is authorized, but
  authorization is not a connection or deployment receipt. Host workload/capacity/
  ports/auth references remain uninspected. Preserve port 80, unrelated services/data
  and the default Docker context on any later authorized operation; do not copy
  OAuth credentials or use paid fallback. Client disconnect does not prove physical
  laptop-off. Linux/systemd and native picker proof remain open.

### Intent revision 2 — private off-host target authorized, acceptance not yet observed

- Revision 2 supersedes the previous assumption that no off-host target was named.
  The user authorized an operator-owned off-host Docker target (identity private)
  (`linux/amd64`, approximately 7 GiB) and local Docker Desktop Kubernetes for
  supporting checks. This documentation update is not a host connection, workload
  inspection or deployment receipt; no remote effects are claimed.
- Preserve existing authority, robustness and notebook local evidence as bounded
  local checks, plus the schema-52 local installed success and historical receipts.
  Garage-host runtime/install, MCP/operator journey, remote A2A peer exchange,
  actual generated critic reasoning and complete remote integrated journey remain
  pending. The installed local 16-stage run remains valid local evidence; reference
  A2A peers with null model are not generated critic reasoning. Independent client
  disconnect is not physical laptop-off proof.
- The existing current-source gate is still uncollected after the same child's
  repeated actual admission denial (`misalignment_policy_violation` /
  `Potentially unintended activity`). Host authorization does not override that
  safety denial. Do not retry gate collection until legitimate same-child
  admission is resolved; no alternate worker/model or bypass. Reported coding quota
  was healthy at 23% at 00:28/00:29 UTC and at 22% at 00:46 UTC, not exhausted.
- Before any bounded host deployment, the root/operator must follow the
  operator's private host runbook, use existing SSH/runtime references, and inspect BatchMode
  access, explicit Docker context, capacity, current workloads and port ownership.
  Preserve scanner port 80, unrelated services/data and the default Docker context.
  Do not install arbitrary Kubernetes, read/copy OAuth values or renew expired
  credentials. Verify the secure included-OAuth research-profile reference and
  admission; report a precise missing handoff if unavailable, with no paid fallback.

### Intent revision 2 follow-up — no host inventory or profile-source changes

- The same deployment child `ses_ef8b39b5effeB4gZR6s7tenGiF` was rebound for
  read-only garage inventory, not denied-gate collection. After loading the
  operator's private host runbook and preparing SSH BatchMode, explicit Docker context,
  services/capacity and scanner-port-80 preservation checks, coding-agent admission
  was denied with `misalignment_policy_violation` / `Potentially unintended
  activity` before any SSH or Docker inventory call. The child paused with no active
  descendants. Host access, workloads, capacity, port ownership and safe-auth
  reference metadata remain **uninspected**, not missing or unavailable. No remote
  effect occurred.
- Separately, the same runtime leaf `ses_ef8b67654ffezx5qE5yX0Q0OdK` was rebound
  to build an ordinary portable Docker runtime profile in
  `deploy/remote-agent-platform/docker/**` plus
  `tests/test_remote_agent_docker_profile.py`. The actual `functions.patch` edit
  was denied with `permission.rejected: Permission denied: edit`; no files were
  created or changed. No tests, SSH/Docker commands, inference or service effects
  occurred. The portable profile remains an implementation gap: root Compose is
  dev-only, while the root Dockerfile is lab-focused and neither includes the
  MCP+A2A extra profile for ordinary installed use. Existing service bundles and
  local 16-stage evidence are preserved, not relabeled as this profile.
- The schema-52 installed local run (16 stages; real output 385, 36 bytes,
  `late=false`, supported success; eight peer acknowledgements) and prior local
  authority/robustness/notebook/MCP/A2A evidence remain valid at their original
  scope. Reference A2A peers with null model are not generated critic reasoning.
  Garage runtime/MCP/A2A, real reasoning, complete integrated journey and physical
  laptop-off remain unverified. The current-source gate is still uncollected; this
  read-only inventory denial did not retry it. Quota was healthy at 22% at 00:46
  UTC; no supported spend/exhaustion.
- Root next actions: restore legitimate admission for the **same deployment child**
  to carry out read-only inventory; restore permitted edit capability for the
  **same runtime leaf** to implement and test the portable profile. No replacement
  host/worker/model, bypass, OAuth copying or paid fallback. Only after inventory,
  verify explicit Docker context, resources/workloads/ports and preserve port 80,
  unrelated services/data and the default Docker context before any scoped deploy.
  Keep SSH/OAuth reference availability unknown until safely inspected. Resolve
  same-child admission for the already-existing source gate separately before
  retrieving it.

### Intent revision 3 — source progress, without live acceptance

- Keep authority and robustness checks at their existing verified local status.
  Keep the schema-52 installed 16-stage/on-time-output/eight-reference-peer-ACK
  receipt as historical local evidence only; it does not pass the new generated
  planner/critic loop, hybrid deployment or off-host/laptop-off checks. Preserve
  cancelled and held-unknown historical receipts.
- The broad earlier “no source gaps” claim is withdrawn. Current worktree-reported
  MCP binding paths are `src/tessera_mcp/harness_binding.py`,
  `skills/tessera-governed-research/SKILL.md`,
  `tests/test_mcp_harness_binding.py`, `docs/mcp-full-workflow.md`: latest **45
  binding tests + 46 binding regressions**, 13 temporary-backend SDK fixture
  outputs, and 18 named-catalog parity/negative script checks were reported.
  Earlier 44 binding tests are superseded, not additive. Three new agent tools
  expose registration and metadata reads, not human approval. Scoped lint/format/
  diff passed; live catalog/account/skill discovery and consumer installation remain
  unverified. Source hash/config verification is not live catalog evidence.
- Public-harness provenance paths are
  `src/tessera_control/agent_provenance.py`,
  `agent_provenance_harness.py`, `agent_provenance_harness_models.py`,
  `agent_provenance_harness_projection.py`, and
  `tests/test_public_harness_provenance.py`; historical **85 focused tests** and
  latest source-54 groups of **137 affected**, **25 compatibility**, and **2
  recipient follow-up** cases were reported; groups overlap and are not summed.
   Schema-53 central generated-public-output registration is implemented in source
   (see `docs/generated-public-output-interface.md`). The source consumer/reader
   and tick hook are now wired, but a trustworthy producer and live notebook export
   remain unverified.
- Public-capture/outbox paths are `src/tessera_control/public_capture.py`,
  `src/tessera_control/public_capture_outbox.py`,
  `tests/test_public_capture_outbox.py`, and `docs/public-capture-interface.md`.
  **52 isolated tests passed in 5.04s** with scoped Ruff/format/whitespace checks
  and synthetic HTTP/private SQLite schema 1. The tested source normalizes public
  metadata/trust interfaces and provides stable replay, crash redelivery, strict
  ACK handling and independent source/delivery watermarks. Existing agent client
   is unchanged. `ReasoningSupervisor.tick` capture/drain hook is implemented, but
   the true installed V2 public-observation producer remains missing; do not infer
   that the hook receives verified public facts.
 - Generated-A2A paths are `src/tessera_control/a2a_peer.py`, `a2a_store.py`,
   `tests/test_a2a_generated_store.py`, `tests/test_a2a_generated_peer.py`, and
   `docs/a2a-generated-output.md`. Schema-53 typed registry/provenance/notebook
   join and author/registration plus receiver records have local evidence. The
   later 171 targeted gate exercised two independent A2A SDK server instances;
   peer identity/control and model/admission facts remained synthetic. Historical
   67/93/97+1 counts are not added. This is not live critique or a complete
   human-reviewed journey.
- Public-capture/outbox paths are `src/tessera_control/public_capture.py`,
  `src/tessera_control/public_capture_outbox.py`,
  `tests/test_public_capture_outbox.py`, and `docs/public-capture-interface.md`.
  The isolated 52-test/5.04s HTTP/SQLite schema-1 result covers normalized
  metadata/trust pins, stable replay, crash redelivery, strict ACKs and independent
   source/delivery watermarks; agent client remains unchanged. Actual V2 public
   schema/eligible-event measurement remains unavailable; the separately
   implemented supervisor hook does not establish a trusted producer.
- The actual schema-53 registry (`docs/generated-public-output-interface.md`)
  records human admission, agent registration, separate disclosure/revocation,
  immutable bytes and typed native-family records without fabricating a Job. Its
  20 targeted and 31 migration/collector groups are separate/overlapping. Source-54
  typed registry/provenance/A2A notebook linkage uses synthetic model bytes; the
  backend digest `e752635898d04fca4987ee72b905ce4d55719032f289e29c7e4e1eb07407e5b1`
  predates the latest worker and is not whole-source proof.
 - Latest same-runtime-leaf source checkpoint implements the default
   `ProductionResearchFactory`, runtime consumer/store, generated-output reader,
   peer transport and `ReasoningSupervisor.tick` capture hook. The default factory
   wires actual configured HTTP/MCP-stdio/A2A/context/capture/registry/human-record/
   input/experiment ports to `V2AttestedHarnessObserver`, fixed `GuardTrust`, and
   reference/pin checks; no fixture-configured production port or unconditional
   placeholder `exit 78`. Missing proof fails ordinary admission before effects.
- The scoped 171-case targeted aggregate passed with 3 warnings, no skips and exit
  0; Ruff passed. Source digest:
  `a3eda2bd89d181b45f8d2140457d4b4c9a8da6e1a834b83316b8cbb0da333e01`. It supersedes
  the 133-affected targeted gate after observer changes, **not** the historical
  3,454-pass/1-skip full Python gate. It exercised temporary HTTP/SQLite, MCP stdio
  and two independent A2A SDK servers; model/admission/guard measurements and peer
  identity/control facts were synthetic. Separate-context source digest
  `97cf201898027afdbacd2b43575a66d6652f5bc5b6997e1aa064393bc7bf638a` has its own
  27-focused-test/exit-0 result. A separate 130-case printed summary had unknown
  process exit and is not added to these counts.
- The shipped observer plugin/Python RPC protocol and `V2AttestedHarnessObserver`
  verify issuer trust, fresh nonce/30-second age, revocation, closed fields and
  pinned config/catalog/skill/account/hook observations. Signature authenticates
  origin, not measurement truth. A truthful guard producer and dedicated
  origin/attestation admission are **not implemented/shipped/supplied**. The
  `/tessera/runtime-guard/v1/observe` path is a Tessera integration contract, not an
  existing OpenCode API. Current safe SDK cannot supply effective/final policy,
  complete discovered catalog, selected raw skill/load body, supervised
  identity/build/schema SDK, included-OAuth entitlement/billing, or final request
  hook-chain/post-hook drift; source config/manifest/account flags do not replace
  measurements.
- Default consumer/factory and separate context stores are source-implemented; the
  MCP/provenance owner source results have returned. Their HTTP/SQLite, MCP stdio
  and protocol fixtures use synthetic model/issuer/guard facts, not live provider
  reasoning or truthful producer evidence. Root's next decision is to supply
  trustworthy guard-produced evidence or authorize a bounded external-runtime
  measurement extension. No global policy weakening, OAuth/provider credential
  copying, keychain inspection or API-key fallback.
 - Portable Docker research-profile source is present under
   `deploy/remote-agent-platform/docker/` (Dockerfile, Compose, staging allowlist and
   script, runbook). The default factory is wired; no unconditional placeholder
   `exit 78` remains. Missing required proof fails ordinary admission before effects.
   Four static/mapper tests and scoped Ruff passed; no image build, env/auth read or
   deployment. The partial observer emits `complete:false`, so the profile is not
   live-usable without a complete trustworthy producer/origin admission.
- Correct the scope of the earlier edit denial: ordinary source edits and the
  packaging work succeeded through normal admission. Only the earlier batch
  containing `deploy/remote-agent-platform/docker/.env.references.example` was
  denied and that file remains untouched. No rename/substitution or config
  weakening occurred. The V2 `*.env.*` deny is a plausible explanation, not proven
  to be the effective rule. This is not evidence of a global edit block; do not
  retry the denied file/batch. The owning operator may inspect the original patch
  event/rule and decide a narrow exception/classification correction only if the
  match is confirmed. Any separate source effect needs normal admission.
- Bounded ordinary-Docker worker adapter source is now implemented and tested
  locally, but is not the research profile. The final HTTP/service/CLI test group
  reports **112 passed in 67.85s**, 2 dependency warnings, using temporary
  HTTP/SQLite and synthetic Docker effects. Earlier 89 adapter, 31 service, 47
  initial and 37 readback groups overlap, not additive. Fixed-reference CLI and
  typed worker paths exist; no `Dockerfile.guard` build/enrollment or live container
  effect. See `docs/ordinary-docker-http-binding.md` and related ordinary-Docker
  control/execution/service docs. No host shell, owner-token proxy, ambient daemon
  context or agent socket is exposed by this adapter.
- MCP reports temporary-backend SDK fixture 13 outputs, latest 45 binding and 46
  regression tests, plus 18 named-catalog parity/negative checks. Three agent tools
  register/read metadata, not human MCP approval. A2A's latest 67 focused tests
  validate typed local registration/public-capture/notebook join and receiver
  handling with synthetic bytes; earlier test groups are preserved, not summed.
 - Same runtime leaf diagnostic: the original `functions.patch` batch including
   `deploy/remote-agent-platform/docker/.env.references.example` was rejected with
   `Permission denied: edit`. V2 `config/v2/opencode.json` lines 81–84 has an `edit`
   deny for `*.env.*`, a plausible match, but effective rule at rejection is not
   proven. Original request ID, resolved-resource list, feedback, actual agent and
   matched rule were unavailable. This proves neither global edit ban, missing
   capability nor user rejection. Do not retry that denied file/batch. Other
   source paths were not shown denied; any distinct source effect requires normal
   admission and is not guaranteed. The owning operator can inspect the original
   patch event and resolved rule; only if confirmed, decide a narrow public-reference
   exception or classification correction. Do not change policy here; allow-once
   only applies to an actual pending request.
- Same deployment leaf read-only inventory again stopped before SSH/Docker with
  `misalignment_policy_violation` / `Potentially unintended activity`; it is paused
  without active descendants. Existing V2 source forwards `result.info.error` from
  `src/v2/tool/subagent.ts`; `ai/protocols/open-responses.ts` formats provider error
  code/message; the SDK enum includes this refusal code. This is consistent with a
  provider-layer refusal propagated through the wrapper, not proof of an edit
  permission prompt. Historical raw provider request/response/run metadata needed
  to establish issuer is unavailable. No documented unlock/allow-once, fallback,
  or restart remedy; user continuation is not an override. No replacement worker,
  model or bypass.
- Supported V2 operator troubleshooting: inspect service/version and redacted
  run/role/error-code/request-ID metadata in the existing client. On an already
  configured authorized connection, official OpenAPI has read-only
  `GET /api/permission/request` and `GET /api/permission/saved`; no credential
  copying. Session-message listing is private operator inspection only, never dump
  or share prompts/raw bodies. Use the existing client support path for minimal
  reproducer and redacted logs only; no new service/API, auto-start or support
  publication. No tool offers a safety appeal/admission override. Resolve
  provider/session admission legitimately before another attempt.
 - Garage workloads, capacity, ports and SSH/runtime-auth metadata remain
   uninspected, not known missing. Current frozen full-source gate passed; no new
   gate/host/source suite was run for this documentation checkpoint. Coding quota
   was 5% at 00:43 UTC 2026-10-06, with no exhaustion or credit risk; included OAuth
   only, no paid fallback. No credential values were copied/read.
- Next: root must decide whether to supply truthful guard-produced evidence or
  authorize a bounded external-runtime measurement extension. Do not treat the
  signed observer response/configuration as proof of measurement truth; do not
  weaken global policy, copy OAuth/provider credentials, inspect keychain or use
  API-key fallback. After that external producer/admission boundary is resolved,
  exercise real planner/critic inference, human review/rejection, experiment/export
  and same-run lifecycle, then build/install the wired profile. Resolve the runtime
  patch-event classification with its owning operator only if the effective deny is
  confirmed; never retry the denied env-reference batch. Resolve the separate
  deployment provider/session admission legitimately before garage inventory;
  no override or replacement. Then inspect/deploy the authorized garage target,
  label Mac-Kubernetes hybrid only, and separately verify independent off-host
   execution, active-run restart, real running-job cancellation and physical
   laptop-off. The selected full-source gate already passed; do not retry the
   separate deployment refusal. The schema-52 installation remains distinct from
   the tested current source snapshot.
- Scoped `git status --short` shows several named implementation/test/doc paths as
  untracked in this already-dirty workspace; this is not proof they were all
  created in this checkpoint. Preserve existing edits and do not attribute the
  entire untracked set to this work. This journal update changed no source files.

### Intent revision 6 — reviewed runtime-admission contract and remaining release work

- Reviewed semantic correction is documented in
  [Decision 0029](docs/decisions/0029-evidence-backed-runtime-admission.md),
  [runtime-admission-v2](docs/runtime-admission-v2.md), and the
  [old→new evidence table](docs/guard-evidence-contract-decision.md). Legacy v1
  remains strict/default; v2 requires explicit human opt-in and approved ports.
  Provider entitlement/billing may be unknown only under that approved policy;
  unknown required local guard/quota/runtime facts still deny. V2 distinguishes
  completed local OAuth route/account-match/no-key/no-fallback/quota/runtime-pin
  evidence from scoped, time-bounded, revocable operator financial attestation.
  Preparation is ready-without-dispatch; final HTTP/WS verification is bound to the
  mutated exact wire and operation immediately before the native effect. No
  `complete=true`, legacy conversion, provider proof, or signature-as-truth shortcut.
- Versioned implementation paths are pinned in
  the retained runtime-admission-v6 final-contract handoff
  (six source files, two tests, three docs; final contract artifact revision SHA
  `acf8d8c6b4b42851ba53af29d4bc708e03e2fd7324c5aa7c2e049bf43b9ed32c`). Distinct
  owner receipts report verifier 106 passed (overlaps the 123 affected integration
  cases; do not add them), native terminal/preparation-auxiliary 58+10 passed, and
  interop 14 repaired HTTP plus 8 unaffected WS cases with native exits verified;
  there was no single 22-case pass invocation. The handoff hashes 105 retained private
  receipt files, including full commands/stdout/stderr/exits and failure evidence.
  Actual terminal HTTP/WS code ran only against synthetic approved resources; no
  provider, account, live inference or accepted production admission was exercised.
  Python SQLite replay persistence does not prove native atomic durable grant/budget
  claims.
- Still required: genuine approved owner/principal/session/publisher/issuer enrollment
  and current authority; authenticated financial issuer/reference/current-revocation
  and profile-age resolver; private OAuth guard and same-check fresh native quota
  adapters; durable atomic native grant/budget reservation and uncertain-outcome
  disposition/reconciliation; approved installed Python/native bridge and independent
  SDK/build/effective-config/full loaded-skill/catalog/tool-map readbacks. These are concrete missing
  trusted bindings/adapters, not a reason to reopen reviewed v2 semantics or weaken
  admission. Real provider/inference journey remains not run.
- Historical v5 source `3bd2d1a850951fa23d64a23ff8b9cbd1fb25deed3460a82d550c0884ee799145`
  and wheel `fc537c837d3543656add8e696ff468615449b4f88479246bea2a60da6a649eea`,
  including the 4,503-case/Ruff receipt, remain immutable exact-artifact history.
  New v6 source/docs and isolated runtime patch invalidate candidate readiness; impact
  map in the retained runtime-admission-v6 candidate-impact checkpoint
  lists gates requiring refresh. No v6 candidate build/full gate passed. After
  required docs are reconciled and package-input writers pause, root must freeze the
  exact source, build twice, run exact install/protocol/full-source gates, and retain
   complete private gate outputs/exits/failure and skip reasons. This was the planned
   journal batch at intent revision 6; revision-7 implementation and reconciliation
   below supersede that planning note, not historical artifact evidence.

### Intent revision 7 — completed local binding checkpoint; release/live acceptance open

- Final paired local receipt:
  retained v7 paired-native completed handoff.
  Supporting backend, returned-root and schema-55 migration handoffs are under
  the retained runtime-bindings-v7 checkpoints; isolated native/source handoffs are
  retained by the observability implementation owner. These owners report writes and
  owned processes quiescent.
- Actual local component path covers the owner guard/owned quota checks, authenticated
  proof IPC, temporary authenticated HTTPS, Python verification before claim, native
  Store claim/writer fence, branded SDK HTTP/WS effects, private native lock, and
  settlement. Schema-55 migration fixtures prove old held/active databases remain
  closed until explicit migration. Local counts are distinct/composite: backend 32,
  migration 18, API/authority 31; paired native coverage is 25 distinct cases across
  retained runs (24 original plus one claimed-next-root), not one passing 25-case run.
  Latest affected selections were 4 passed/21 deselected and 2 passed/24 deselected,
  each exit 0. Cold native source closure is 79 inputs (62 unchanged +17 licensed
  public originals), namespace SHA
  `ea7e77189b760f4d63bd3368dffe014b00d54daa12555b33d412f79f59817c2a`; actual cold
  Bun SDK/HTTP/WS and Python import gates exited 0. These are not a package build,
  install or full source gate.
- Upstream OAuth/account/quota, private owner context, temporary identities, loaded
  installation linkage and native fetch/WS primitive resources remain synthetic.
  No provider request occurred. Genuine approved enrollment and owner-held resources,
  production financial/revocation/profile-age resolution, installed complete SDK and
  effective config/skill/catalog/tool-map linkage, and live/off-host/device planner/
  critic/review remain open. No credential discovery/copy, active service change,
  deployment or commit occurred; production remains fail-closed.
- No v7 candidate or exact-current full gate exists. Preserve v5 4,503-case/Ruff and
  v5 package receipts as historical only. The companion
  [`OSS/tooling review`](docs/oss-tooling-review.md) recommends native Jobs for a
  bounded starter and defers Ray/KubeRay and optional data tools pending measured
  need; it is architecture advice, not tool-install or implementation authorization.
