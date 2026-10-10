# Local API comparison workflow

This developer integration example drives ordinary CLI/authenticated API,
dispatch consumer, adapter journal and artifact storage. Default tests fake only
external Job/Pod effects and output bytes; they do not execute scientific work,
contact providers or establish live cluster support. The baseline/intervention
plans differ, and adverse/unknown outcomes are covered. No SQL-seeded authority
or supervisor-impersonated terminal observation is used.

```sh
.venv/bin/pytest -q tests/test_local_api_workflow.py
```

The executable `Journey`/`retain_evidence` examples use temporary state and
synthetic credentials. Live checks remain separate opt-ins.

## Ordinary command sequence

All HTTP mutations require authentication and `Idempotency-Key`. Reuse the same
key and exact body after a lost reply; read current revisions before decisions.

1. `POST /projects`; inventory/adopt public local inputs with `tessera workspace
   inventory` and `tessera workspace adopt` using the reviewed digest. Adoption
   is a local operator action, not an arbitrary server-path upload API.
2. Enroll a scoped worker at `POST /credentials`, register via
   `POST /projects/{id}/workers`, separately release inputs at
   `POST /snapshots/{id}/releases` and configure bounded execution limits.
3. Create two tasks at `POST /projects/{id}/tasks`, immutable plans at
   `PUT /tasks/{id}/plan`, then register baseline/intervention plan/input digests
   at `POST /projects/{id}/experiments` **before dispatch**.
4. Human-approve exact plans at `POST /tasks/{id}/approve`; approval creates no
   execution authority. Separately issue a bounded grant and bind its variant at
   `POST /experiment-variants/{id}/admitted-attempts`. Worker approval, stale
   design admission and unapproved plans are rejected.
5. Worker heartbeat/acquire `/worker/leases`; request lease `/package` with the
   admitted attempt and `kubernetes-job` adapter. A human separately approves
   `/experiment-admitted-attempts/{id}/dispatch` against current packaged revision
   and explicit target digest.
6. `KubernetesDispatchWorkerService` claims
   `/worker/kubernetes-dispatches/next/claim`, persists intent, dispatches and
   reports its own observations. Lost-create reconciliation uses stable identity;
   restart uses the same private workspace. Unknown is not permission to replace
   a submission. Idle replay stays idle; claim replay rechecks original authority.
7. Resolve admitted attempts through `/resolve`. Normal completion requires
   verified Job/owned-Pod termination evidence; premature success is rejected.
   Failure is human bookkeeping, not scientific rejection. Cancellation needs
   both request and absence proof; uncertain termination retains exposure.
   Later cancellation does not erase an earlier human `unknown`.
8. Upload declared bytes through lease `/artifacts`, commit `/project-artifacts`
   and exact `/artifact-manifest` using the same control-issued operation, lease,
   worker and epoch. Never substitute a CPU-derived/fabricated ID. Manifest digests
   include actual operation identity; historical records are not backfilled.
9. Link retained artifacts via `/experiment-attempts/{id}/artifacts`, record human
   `/measurements` and review evidence-citing conclusions. `scientific_validity=
   not_assessed` stays explicit even after review.
10. Record manifest `/dispositions` with `retain_quarantined`, then a separate
    `/custody-releases` action before retrieval. Compare complete
    `/experiments/{id}/export` and exact bytes after reopening; adverse outcomes,
    operations, evidence and human review must survive.

## Limits

The default example injects output bytes through the ordinary API; it does not
verify target isolation/quota or automatic live collection. Terminal observations
and human outcomes are distinct; neither accepts science. General provider/model
use, accurate billing, unattended renewal and laptop-independent deployment remain
unestablished. See [ordinary collection](ordinary-worker-collection.md) for the
operator collector and [capabilities](implemented-capabilities.md) for wider limits.
Held restore preserves history, never execution authority; follow [recovery](recovery.md).
