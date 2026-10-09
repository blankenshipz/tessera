# 0028 — Historical pre-Job preparation recovery

This distinct recovery path preserves the historical `create_intent:true`.
It does not widen bound_job_intent/pre_manifest zero-create contracts or let a
human bypass a lost/expired original worker with a broad create-intent override.

The original active worker may POST
`/worker/execution-grants/{grant_id}/attest-preparation-not-submitted`, bearer
worker audience and Idempotency-Key, with the strict `pre_job_preparation` proof
from the installed Kubernetes preparation-recovery CLI and expected_revision.
The proof additionally requires `worker_attests_original_exclusive_stopped:true`:
the original submitting service must be stopped, the original journals exclusive,
and durable cancellation fenced before provider reads. The CLI's explicit
`--exclusive-stopped-worker` assertion is this operational precondition; clients
must transmit it rather than assume it by default.

Proof requires the exact package/operation/lease/epoch/target and journal binding
and manifest digests, a succeeded original pinned preparation Pod's UID/manifest
digest, its absent durable result receipt, and the fixed
`ordinary-output-prepare-before-job-v1` gate plus `job_request_not_reached:true`.
This relies on that known implementation committing the preparation result before
returning to its Job request. Missing receipt alone, on an unknown implementation
or an active/concurrent/replaced workspace, is insufficient. It is original-worker
attested evidence; control cannot independently inspect retained files, prove the
helper effect-free, or verify submitted Kubernetes readbacks.

`preparation_journal_pod_uid` must be nonblank and equal `preparation_pod_uid`.
The worker proof generator must reject a missing original helper journal UID;
a current matching manifest at the deterministic name is not proof that a deleted
helper was not replaced. The first recorded preparation identity/digests are
immutable across subsequent fresh attestations. Neither field may be inferred
from a new current Pod when the retained original journal has no UID.

The fresh readbacks require bound namespace UID, deterministic Job-name absence,
and complete execution-operation-labelled Job/Pod absence. The execution selector
is exactly `tessera.dev/operation={operation_id}` (32 hexadecimal characters).
The verified preparation helper instead uses
`tessera.dev/output-operation={operation_id}` and deterministic name
`tessera-output-prepare-{operation_id}`. `operation_pods_absent` means absence
under the **execution** selector only, not absence of all output/helper Pods.
Only this exact preparation helper is accounted separately; no generic arbitrary
Pod exclusion, cleanup or execution-Pod relabelling is allowed.

Human grant cancellation remains separate. Attestation requires the cancelled
original grant revision and dispatch/lease/epoch, current original worker credential,
no provider identity, and nothing beyond unconfirmed original-worker unknown
observations. Readbacks must be at most 30 seconds old (5-second future skew).
Attestation replays recheck these fences and do not refresh evidence timestamps.

The **original human grant creator** may separately POST
`/execution-grants/{grant_id}/reconcile-attested-preparation-not-submitted`,
human bearer and Idempotency-Key, with
`{expected_revision,operation_id,attestation_digest}`. Control revalidates that
exact immutable worker event, bindings, still-active worker credential and readbacks
at most 60 seconds old before a CAS-fenced zero-settlement held-reservation release.
Grant state, accounting, distinct event and idempotency response commit atomically.
A successful same-key owner replay returns its original receipt, not new authority.

Original operation/provider/observed state and worker journal intent are never
rewritten. The helper's succeeded phase is not workload completion or provider
termination; helper resources/effects still exist and require separate owned cleanup
and accounting. This is operational recovery, not scientific acceptance or new
submission permission. Any adverse execution/provider observation blocks release.
