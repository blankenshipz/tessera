# Local API comparison workflow and remediation gates

## Supported boundary

This is a **developer integration slice**, not a supported live Kubernetes,
model/provider, or scientific workflow. The default test drives the ordinary
CLI, authenticated API, dispatch consumer, adapter journal, and local artifact
store. Only external Job/Pod effects and output bytes are synthetic. It uses two
different immutable plans: a registered baseline and intervention. Both execute
through the worker path in the success case; the intervention is also tested as
failed, cancelled, and unknown. No test inserts authority through SQL or posts a
made-up terminal observation on the supervisor's behalf.

Run with the already installed development environment (no cluster, Docker,
provider, or credential setup):

```sh
.venv/bin/pytest -q tests/test_local_api_workflow.py
```

The fixture initializes only temporary state and synthetic credentials. It does
not execute the plan's command, compute a scientific metric, contact a network
provider, or inspect a kubeconfig. Do not use its synthetic output as research
evidence. Live harnesses remain separate explicit opt-ins outside default tests.

## Ordinary command sequence

All HTTP mutations require authentication and a unique `Idempotency-Key`; reuse
the same key and exact body after a lost reply. Use revisions returned by fresh
reads, not a stale approval panel. The executable example is
`tests/test_local_api_workflow.py` (`Journey`, `retain_evidence`, and the four
comparison cases).

1. Create a project with `POST /projects`. Inventory and explicitly adopt public
   local inputs using `tessera workspace inventory` and `tessera workspace adopt`
   with the reviewed inventory digest. Adoption is a local operator CLI action;
   no arbitrary-path upload API was introduced.
2. Enroll a scoped worker credential with `POST /credentials`, register it at
   `POST /projects/{id}/workers`, and separately authorize the input snapshot at
   `POST /snapshots/{id}/releases`. Configure bounded execution limits.
3. Create two tasks and immutable plans with `POST /projects/{id}/tasks` and
   `PUT /tasks/{id}/plan`. Register their plan and input digests as the baseline
   and intervention using `POST /projects/{id}/experiments` **before dispatch**.
4. Human-approve each exact plan via `POST /tasks/{id}/approve`. This creates no
   execution authority. Separately create a bounded execution grant and bind it
   to its variant with `POST /experiment-variants/{id}/admitted-attempts`.
   Unapproved plans, worker approval, and stale design admission are rejected.
5. The worker heartbeats, acquires `/worker/leases`, and requests the ordinary
   lease `/package` with the admitted attempt ID and `kubernetes-job` adapter.
   A human separately approves `/experiment-admitted-attempts/{id}/dispatch`
   using the current packaged revision and explicit target digest.
6. `KubernetesDispatchWorkerService` claims assigned work through
   `/worker/kubernetes-dispatches/next/claim`; no fixture-provided dispatch ID is
   required. It persists local intent, invokes the injected adapter, reconciles
   a lost create reply by stable identity, and reports its own observations.
   Restart uses the same private worker root; an unknown operation is not a
   replacement submission. Replaying an idle claim stays idle; replaying a
   package claim rechecks the exact original dispatch's current authority.
7. Resolve the admitted attempt explicitly using `/resolve`. Normal completion
   now carries verified Job-and-owned-Pod termination evidence (decision 0015).
   A success label before terminal evidence is rejected. Failure remains a human
   outcome, not automatic scientific rejection. Cancellation requires a request
   **and** the adapter's separate absence proof; a failed cancellation remains
   unknown/unresolved and cannot release exposure by asserting termination.
   Later confirmed cancellation does not erase an earlier human `unknown`.
8. Upload declared synthetic bytes through the lease `/artifacts` route, commit
   `/project-artifacts`, and create the exact `/artifact-manifest`. All three use
   the same control-issued operation, lease, worker, and epoch. A CPU-derived or
   fabricated ID cannot replace a journaled Kubernetes operation. The manifest
   digest includes the actual operation ID. Legacy unjournaled CPU compatibility
   remains unchanged; no historical receipts or manifests are backfilled.
9. Link retained artifacts and record explicitly human measurements through
   `/experiment-attempts/{id}/artifacts` and `/measurements`. A human may record
   and review an evidence-citing conclusion. `scientific_validity=not_assessed`
   remains explicit even after review.
10. For private byte retrieval, record `retain_quarantined` at the manifest's
    `/dispositions` route, then explicitly `/custody-releases`. Retrieval is
    denied before release. Recreate the control app and compare the complete
    `/experiments/{id}/export` plus retrieved bytes: attempts, adverse outcomes,
    operations, dispatches, evidence, and human review survive reopening.

## Review remediation roadmap

The finding identifiers below come from the repository's dated comprehensive
source-review crosswalk in `IMPLEMENTATION_TASKS.md` (F1–F9). Historical checked
tasks are prior evidence, not a fresh pass or remote acceptance on this dirty
tree. This pass does not reinterpret the separate architecture-review document
as a newly completed review.

| Priority / gate | Findings and repository evidence | Acceptance / next milestone |
|---|---|---|
| **L0: local comparison** | **F9** missing ordinary admission-to-evidence journey; older integrated test hand-posts observations, live lab fixture executes only baseline. This pass connects normal termination reporting and journal-aware artifact placement/manifests. | Four API comparison cases, both registered plans, one operation per attempt, durable retained bytes/review, explicit negative/unknown outcomes, no SQL-seeded authority or fake live claims. Default synthetic acceptance only. |
| **L0: execution safety** | **F1** expiry overrun, **F2** stale registry heartbeat, **F3** escaped observation errors: T01–T03 record local repairs; dispatch/execution tests exercise them. Incomplete terminal Job/Pod evidence is now unknown. | Hard/lease/disconnect bounds survive delay/restart/clock rollback; registry health stays distinct from leases; read failures retain exact-operation ownership and cancellation responsibility; no replacement submit after lost reply. Selected-target proof still required at R1. |
| **L0: durable integration** | **F4** historical schema-43 failures; this comparison slice originally used schema 47 and recorded schema 50 as current at that time. Current source is schema 55; the latest recorded installed journey remains schema 52. | Scoped evidence and dispatch backup/held-restore regressions are dated local results, not a fresh full gate. Before integration-ready declaration run the full CONTRIBUTING gates on a coordinated tree, including migration preservation/rollback and desktop checks where relevant. Held restore never grants active execution. |
| **L0: honest capability/docs** | **F8** schema/capability/backup drift: README and progress already contain corrections, but component tests must not become live claims. | Each capability identifies default fake vs opt-in live evidence, schema/custody/recovery limits and unrun checks. This note, README, progress and decision 0015 distinguish worker completion, cancellation, custody and human scientific judgment. |
| **M1: normal coordinator** | **F5** ordinary independent consumer gap; provider-disabled coordinator/claim seams exist in the current tree. | Fresh request consumed through ordinary independently hosted processes, scoped identity, bounded retries, health/unavailable projection, durable restart and no duplicated effects. Deterministic mode remains explicitly separate from model activation. Do not build a new coordinator architecture first. |
| **M1: desktop journey** | **F7** remote-workflow projection and **F9** user walkthrough gaps; dirty desktop bridge/renderer work already exists. | Wire the accepted API comparison to explicit plan approval, separate admission/dispatch, evidence/review and stop actions. Test renderer/preload/main end-to-end, disconnect/reopen, freshness, pending decisions, and connection/project fencing. Do not equate UI status or fixture turns with remote execution. |
| **R1: selected remote lab** | **F6** incomplete remote runtime/deployment and **F9** setup/reconnect walkthroughs; T07–T13/T18 remain selected-target gates. | Name one always-on control home, worker, operator, public workflow, exact versions, credential custody/renewal and persistent input/output/journal storage. Prove no laptop relay/login/volume dependency. Separately authorize bounded live isolation, quotas, restart, F1–F3 faults, cancel/unknown, backup/held recovery, and proposal-to-evidence plus reconnect/stop walkthroughs. |
| **M2: expansion only after pilot** | Remaining M1 operational/provider/collaboration packages and product hypothesis in DESIGN §8. | Measure total researcher setup/intervention/review/verification/recovery effort and all outcomes before broad skills/cloud/desktop expansion. Real model/provider activation needs its own credential, usage/billing and cancellation gate. Shared organizations, HA/active failover, publication, signed distribution/license/security-release prerequisites are later explicit gates, not implied by single-owner alpha tests. |

## Remaining boundaries and next bounded task

### Verification recorded for this pass

On the dirty working tree (no commit or clean-tree claim), the following passed:

```sh
# 140 passed, 2 existing Starlette/httpx and anyio deprecation warnings
.venv/bin/pytest -q tests/test_local_api_workflow.py tests/test_kubernetes_dispatch_recovery.py tests/test_kubernetes_execution.py tests/test_artifact_manifests.py tests/test_project_artifacts.py tests/test_result_checkpoints.py tests/test_artifact_custody.py tests/test_integrated_experiment_workflow.py tests/test_goal_to_decision.py tests/test_workers.py tests/test_execution_contracts.py
# 62 passed, same 2 warnings
.venv/bin/pytest -q -o faulthandler_timeout=30 tests/test_kubernetes_dispatch.py
# Both Ruff commands passed: 9 files
.venv/bin/ruff check src/tessera_control/artifacts.py src/tessera_control/artifact_manifests.py src/tessera_control/project_artifact_store.py src/tessera_control/experiments.py src/tessera_worker/kubernetes_execution.py src/tessera_worker/kubernetes_dispatch_service.py tests/test_local_api_workflow.py tests/test_kubernetes_execution.py tests/test_kubernetes_dispatch.py
.venv/bin/ruff format --check src/tessera_control/artifacts.py src/tessera_control/artifact_manifests.py src/tessera_control/project_artifact_store.py src/tessera_control/experiments.py src/tessera_worker/kubernetes_execution.py src/tessera_worker/kubernetes_dispatch_service.py tests/test_local_api_workflow.py tests/test_kubernetes_execution.py tests/test_kubernetes_dispatch.py
git diff --check
```

Running those twelve pytest files together initially timed out after 300 seconds
without a failure report. Collection confirmed 202 tests. A diagnostic command
`.venv/bin/pytest -vv -o faulthandler_timeout=30 tests/test_kubernetes_dispatch.py
-k 'pending_renewal or failed_lifecycle or periodic_lost'` passed 5 tests (57
deselected); the split commands above then passed. The aggregate timeout remains
unexplained, not a passing combined gate. Earlier focused tests caught the
placement/manifest operation mismatch and test setup mistakes; those were repaired.
Initial lint found long lines; scoped formatting and line wrapping repaired them.
No full-repository, desktop, or live-provider gate was run.

### Limitations

No automatic target byte collector, real cluster isolation/quota result,
provider/model execution, accurate billing, scientific validation, unattended
credential renewal, or laptop-independent deployment is established here. The
worker returns a collection handoff; the test injects output bytes through the
ordinary upload API. Control's terminal observation records exit/termination,
not a process exit code: `succeeded`/`failed` remain human bookkeeping decisions.
Synthetic checkpoint/result-acceptance APIs retain their separate narrower
contract; this slice uses admitted-attempt evidence and human conclusions.

Next: one bounded desktop/API integration task, preserving the existing bridge
work, to present these exact approval/admission/dispatch/evidence/stop boundaries
and replay the same two-variant journey with fake effects. Do not expand target
provisioning or activate providers to make that UI demo appear complete. Full
integration gates and then separately authorized selected-target acceptance are
required before a release or remote-pilot claim.
