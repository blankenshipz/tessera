# Ordinary Docker control-plane contract (historical source schema 54)

This is backend source acceptance, not Engine enforcement, live off-host execution,
or laptop-off proof. Runtime preflight/witnesses remain required. Docker enrollment
is a human declaration of bounded capability/placement, not a fabricated witness.
Generated-public-output registration and schema-53 history are unchanged.

This is a schema-54 source milestone, not the current schema. Source is now schema
55; the latest installed journey receipt remains schema 52. Schema-55 migration adds
runtime-binding records and does not establish Docker Engine enforcement or a new
installed-worker acceptance. See the [runtime-binding contract](runtime-bindings-interface.md)
and [release checklist](release-checklist.md).

## Human enrollment and approved task

POST `/projects/{project}/docker-targets` using a separately authenticated human
credential and stable `Idempotency-Key`:

```json
{
  "worker_principal_id": "actual_enrolled_worker",
  "target": {
    "daemon_id": "fixed_engine_id",
    "endpoint_digest": "64hex",
    "image": "approved/repository@sha256:64hex",
    "os": "linux",
    "architecture": "amd64",
    "guard_version": "ordinary-docker-guard-v1"
  },
  "resources": {
    "cpu_millicores": 100,
    "memory_bytes": 67108864,
    "pids_limit": 16,
    "scratch_bytes": 1048576
  },
  "max_output_bytes": 1024,
  "network": "none",
  "placement": "independent_execution_host",
  "isolation_approved": true,
  "expires_at": "offset timestamp within 24 hours"
}
```

Returned `id`, `revision`, `target_digest`, and `bounds` are retained for operator
configuration. POST `/docker-targets/{id}/revoke` with `{expected_revision}` fences
fresh worker authority without claiming any container has stopped. No global or
default Docker target exists. No endpoint/socket secret is returned or given to a
workload; only its approved digest is recorded.

The ordinary human-reviewed task `execution_spec` uses `profile:"ordinary-docker"`
and `docker:{target_id,target_digest,cpu_millicores,memory_bytes,pids_limit,
scratch_bytes}`. Existing argv/snapshot/output paths/output byte bound/network-none
fields remain. Docker pins are forbidden on other profiles. Human task approval,
snapshot release, ready worker enrollment, execution limits and execution grant
are all still required. Grant image must equal the enrolled immutable image;
resources/output bound cannot exceed enrollment, and grant cannot outlive target.
Unsupported shell/proxy argv and output paths cannot produce a native package.
No task approval is inferred from target enrollment.

## Agent admission and native package/dispatch

Use existing human-issued bounded agent authority with the exact plan/task/variant/
execution grant/revision/epoch/target/destination/model/budget pins. Agent consumes
`attempt:admit` through `/agent-authority-grants/{id}/consume`. Native Docker package
issuance requires that real agent admission consumption; its principal is resolved
from the consumed credential, never from a caller-provided agent label.

Worker POST `/worker/docker/leases/{lease_id}/package` with
`{expected_epoch,admitted_attempt_id}` and a stable idempotency key. This returns
`{package,package_digest,lease,target_id}`. `package` is the exact strict
`tessera_worker.ordinary_docker_contract.DockerPackage` JSON, with no Kubernetes
fields, shell/mount/env/socket extensions, or Job/Pod UID. `lease` is precisely the
six-field `DockerLease` JSON. Hashing matches the worker contract. Operation intent
and immutable admitted-attempt binding commit before reply; this is not dispatch.
The standard local-CPU/Kubernetes package route does not enable Docker implicitly.

Agent consumes `attempt:dispatch` with `{attempt_id,expected_revision,target_digest,
profile_version:"ordinary-docker-v1"}` through the same typed authority route.
Budget reservation/consumption/event/idempotency remain its existing transaction.
Alternatively a separately authenticated human POSTs
`/experiment-admitted-attempts/{attempt_id}/docker-dispatch` with those three fence
fields and profile. It cannot widen the approved package or target.

## Worker entrypoint protocol

All endpoints below require the actual assigned worker credential, not agent or
human credentials. Worker ready state, current human and agent authority lineage,
tenant/task/plan, monetary/concurrency limits, grant/lease expiry, epoch and target
are rechecked. No operation replacement is admitted by reconnect/replay.

1. GET `/worker/docker/dispatches?target_digest=64hex` returns up to 100 matching
   owned queued/claimed/running/unknown records: `{dispatches:[{fence,package,lease,
   state}]}`. Expired/revoked records remain durable rather than being rekeyed.
2. POST `/worker/docker/claim` with `fence` and stable key returns
   `{package,package_digest,lease}`. Restart can retrieve the same running/unknown
   descriptor without resetting state, provider identity, or one-shot intent.
3. The adapter's trusted callback POSTs `/worker/docker/current-authority` with
   `fence`; returns `{lease,authority_current:true}` only for current permission.
   This read has no idempotency key/cache revival. Map `lease` with
   `DockerLease.model_validate_json(...)`. It is authority, not Engine health.
4. Renewal/heartbeat remain the ordinary `/worker/leases/renew` and
   `/worker/heartbeat` commands. The existing worker adapter journals stable keys.
5. POST `/worker/docker/report` with stable key, `fence`, plus `container_id`
   (actual 64hex ID or null for unknown), `state` (`running`, `succeeded`, `failed`,
   `cancelled`, `unknown`), `termination_confirmed`, `stop_requested`, `exit_code`.
   Running requires exact container ID. Success requires confirmed exit zero and
   no cancellation; failed requires confirmed nonzero exit; cancelled requires
   an observed exact-ID stop and confirmed termination. Unknown cannot infer stop.

`fence` is the closed body `{operation_id,admitted_attempt_id,lease_id,
expected_epoch,package_digest,target_digest}`. Container ID is immutable once
recorded. Reports from the original claimed worker remain allowed after revocation
to preserve honest stop/late/unknown evidence at the original immutable epoch;
they grant no renewed execution rights. Terminal observations cannot be rewritten.
All observations/events/idempotency commit atomically and remain attributed worker
evidence, not independent provider proof. Scientific validity stays `not_assessed`;
human experiment resolution and settlement are separate.

## Output custody and recovery

### Original-worker stop/readback and report receipts

POST `/worker/docker/readback` with the existing six-field `fence`, optional
`expected_container_id` (64hex), and optional `delivery_key`. No idempotency key is
required: this is a same-snapshot read, not claim/renew/execute. The response is
the closed `DockerReadbackReply` contract in `ordinary_docker_models.py`:

- exact `fence`, `task_id`, `grant_id`, `worker_principal_id`,
  `claimed_by_principal_id` (null until claimed);
- recorded `container_id` or null, `desired_state` (`admitted` or
  `cancel_requested`), actual `dispatch_state` and `dispatch_revision`;
- `can_execute`, `authorization` (`execute` or `stop_only`) and
  `effective_action` (`run` or `stop`), plus the original six-field `lease`;
- `latest_report` (closed `DockerReport` or null), `latest_delivery` (or null),
  requested `delivery_key`, and exact keyed `delivery` (or null if uncommitted).

`DockerDeliveryReceipt` is `{idempotency_key,payload_digest,response_status:200,
response}`. `response` is the actual persisted `DockerReportAck` containing the
exact native report plus `scientific_validity:not_assessed` and
`human_resolution_required:true`. Digest is the canonical normalized report body,
not the readback request. A keyed receipt for another operation or mismatched
container/pins fails closed; a missing key yields null, never synthetic ACK. An old
running receipt remains an ACK of that old report even if `latest_report` is now
cancelled. Restart must retain its original report key/body and compare the keyed
receipt before draining a pending delivery. Readback never repeats provider effects.

Original assigned workers with **currently valid worker credentials** can read
their original operation after lease/grant/target expiry or revocation, or after
terminal report/settlement. Changed execution authority yields `stop_only` and
`effective_action:stop`, even if the recorded desired state is still `admitted`.
It does not rewrite that fact, revive a lease, renew a grant, permit a new claim,
or infer termination. Active execution still needs all current-authority checks.
The container ID, original task/attempt/package/target/lease/epoch/hard deadline,
claim and tenant are validated independently of the active permission check.
If the server has no container ID, it cannot validate a non-null expected ID;
omit that optional pin only to obtain the honest null/unknown snapshot. The worker
must still use its exclusively owned journal and exact container labels for stop.

Expired/revoked **credentials** remain 401; neither readback nor receipt lookup
accepts an old token or human-as-worker proxy. Separately authenticated human
maintenance/recovery must use its own already approved scope, or an explicit secure
original-identity recovery must be provided separately. This endpoint does not
implement or claim that recovery bridge. No new schema or installed migration is
needed: report delivery proof comes from existing transactional idempotency rows.

After bounded declared collection, use existing worker artifact upload, project
placement and `/worker/leases/{lease}/artifact-manifest` routes with the actual
Docker `operation_id`, original lease/epoch, path/size/hash and bytes. Their operation
binding is provider-neutral. Native reporting records `provider_id=container_id`;
do not fabricate Job/Pod identities. Outputs remain quarantine/unclassified until
the existing explicit custody/review boundaries authorize them. Worker adapter
enforces the declared output path/aggregate byte bounds; generic control artifact
intake retains its existing per-artifact limits and custody checks.

No observation releases a reservation. Separately authenticated human ordinary
grant reconciliation requires current confirmed termination evidence, honest
explicit settlement and expected revision; unknown evidence still blocks it.
Human no-start reconciliation can release a revoked native package only if no
Docker/Kubernetes dispatch row, ever-queued Docker event, provider identity or
operation reconciliation exists. Absence is checked in the same write transaction.
Any possible dispatch remains unresolved; no provider termination is inferred.

## Migration and remaining integration

Source schema **54** explicitly adds empty `ordinary_docker_targets` and
`ordinary_docker_dispatches`; `tessera migrate --state-dir STATE` is required before
updated service readiness. Only active backup-success freshness resets;
held-restore credentials/history/metadata and write fences remain intact. This
worker did not migrate installed state or edit the generated registry.

This schema-54 checkpoint's worker-CLI integration and target tests remain separate
from later runtime-binding source work. Image/runtime witnesses, real output, running
restart/cancellation, off-host and laptop-off remain separate acceptance. The test
fixture migration was reconciled for schema 55 in the linked schema-55 owner handoff;
this dated receipt is not rewritten. No peer/provenance/agent runtime source was
edited in the original schema-54 slice.
