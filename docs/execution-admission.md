# Local CPU admission records

Schema v6 adds project execution limits, input release decisions, execution grants,
worker leases, observations and reconciliation. These routes commit authority and
accounting records. A separate [input delivery](input-delivery.md) route transfers
released bytes under a live lease. Container submission, harness startup and
artifact collection are not connected; the CPU supervisor remains separate.
No paid or real research jobs were used to validate this slice.

Schema v19's authenticated worker-only
`POST /worker/external-operations/{operation_id}/receipt` accepts bounded,
canonical, digest-checked evidence from the injected `fake` provider only. It
fences tenant, worker, lease, epoch and immutable provider identity. It updates
only the receipt's observed state and cannot settle cost, accept a result, or
release an artifact. v18 operations and legacy leases migrate with no receipts.

Schema v36 adds an explicitly human-approved, provider-disabled Kubernetes
dispatch outbox for an already packaged admitted attempt. The control plane
stores the immutable package response, then binds its stable operation ID,
admitted attempt, enrolled worker, lease/epoch, exact target digest and profile
before a worker can claim it. Every claim replay, including one using the same
idempotency key after a lost reply, rechecks the current grant, lease expiry and
ready-worker state before returning the same control-issued package. It does not
create identities, select a current context, provision a target, or invoke
Kubernetes. Grant revocation fences queued/claimed dispatch effects in the same
transaction while original-operation observations remain available for
reconciliation. The fake-client worker service supervises its one durable Job:
it rechecks authority, reconciles long-running status, requests cancellation on
denial/lease deadline, and never uses a replay or restart to create a replacement
Job. Its report journal scopes immutable payload bytes and idempotency keys to
the dispatch and operation, so a lost report reply is replayed exactly after
restart. Collection remains metadata-only and export retains the dispatch to
canonical admitted-attempt/operation linkage. This is not live-cluster support.

Schema v21 adds human-created, metadata-only credential-access leases. They are
separate from Tessera authentication credentials and bind one opaque `secret_ref`
to the current grant, execution lease, worker and epoch. The lease is provider
neutral (`audience`, `purpose`, unique `scope` entries), may not outlive its parent
execution lease, and is not attached to an external-operation receipt. The control
plane never receives, stores, logs, returns, or records the secret value.

## Human admission sequence

1. Adopt a snapshot and review its exact digest and contents.
2. Enroll a project-scoped worker credential with the `worker` audience.
3. Create a destination-specific snapshot release with explicit human review.
4. Revise the task with an executable local CPU specification and approve its
   current revision and plan digest.
5. Configure project execution limits and issue a grant for the exact task,
   worker, snapshot and pinned image.

| Human/control route | Required command fields |
|---|---|
| `POST /snapshots/{id}/releases` | expected_digest, worker_principal_id, classification=`public`, permits_worker_transfer=`true`, rationale, expires_at |
| `GET /snapshots/{id}/releases` | Bounded offset/limit |
| `POST /snapshot-releases/{id}/revoke` | expected_revision |
| `PUT /projects/{id}/execution-limits` | expected_revision (0 for creation), currency, monetary_limit_cents, max_concurrency |
| `GET /projects/{id}/execution-limits` | Returns configured limits and held/settled amounts |
| `POST /projects/{id}/execution-grants` | task_id, expected_task_revision, expected_plan_digest, worker_principal_id, snapshot_id, pinned_image, currency, max_exposure_cents, expires_at, acknowledges_unmeasured_infrastructure=`true` |
| `GET /projects/{id}/execution-grants` | Bounded offset/limit |
| `GET /execution-grants/{id}` | Current grant record |
| `POST /execution-grants/{id}/revoke` | expected_revision |
| `POST /execution-grants/{id}/reconcile` | expected_revision, settlement_cents, provider_terminated=`true` |
| `POST /execution-grants/{id}/credential-access-leases` | execution_lease_id, expected_epoch, opaque secret_ref, audience, purpose, scope, expires_at |
| `GET /projects/{id}/credential-access-leases` | Human-only bounded metadata list |
| `POST /credential-access-leases/{id}/renew` | expected_revision, expires_at within the current parent lease |
| `POST /credential-access-leases/{id}/revoke` | expected_revision |

All mutations require `Idempotency-Key`. Control routes reject worker credentials;
worker routes reject human/control credentials. Lists use limit 1–200 and offset.

Only an explicitly reviewed `public` classification is supported for release in
this pilot. The release binds one worker and does not relabel the immutable source
manifest or certify provenance. It cannot outlive the reviewing identity or worker
credential; expiry is at most 24 hours. Revoke a previous release before replacing
it. A replacement release cannot silently renew an old grant: grants bind the
original release ID. Releases provide no standalone content-download endpoint.

An execution grant requires an approved executable plan with the same retained
input digest. It freezes the worker, image repository plus SHA-256 digest, input
release, task revision/digest, currency, exposure and expiry. Nonempty pinned skill
lists are rejected until verified skill materialization is implemented. The only
supported profile is `local-cpu` with network disabled. Plan approval itself still
does not issue a grant. Task approval receipts retain their `execution_granted=false`
field; consult the separate grant and lease records for admission state.

Project monetary limits use explicit three-letter currency labels and integer
hundredths (`*_cents`); there is no exchange-rate conversion or provider invoice
integration. Held plus settled amounts cannot exceed the limit when admitting new
work. The same check applies to cumulative settled costs for the task across all
plan revisions, plus its held/new reservations, against the current approved task
budget. A plan revision does not reset spent money. Concurrency is 1–8 unresolved
grants per project, with at most one unresolved grant per task. Currency cannot
change while exposure or unresolved grants remain.

Local CPU infrastructure cost is unmeasured. A grant therefore requires explicit
acknowledgement of that exposure; the recorded monetary reservation is not a
provider-enforced hard cap. Settlement preserves reported overages, even when
larger than the reservation. Those overages block new admission/renewal rather
than being clipped or erased. This slice records held, released and settled
amounts; incurred/estimated-unbilled/disputed usage feeds remain unimplemented.

## Worker lease and observation sequence

| Worker route | Command |
|---|---|
| `POST /worker/leases` | grant_id |
| `POST /worker/leases/renew` | lease_id, expected_epoch |
| `POST /worker/observations` | lease_id, expected_epoch, operation_id for journaled leases, observed_state, observed_at, optional provider_id, termination_confirmed |

A lease lasts at most 30 seconds. Its fixed hard deadline is the earliest of grant
expiry, worker credential expiry, and initial lease time plus approved task duration.
Renewal cannot reset that deadline. A grant can acquire only one lease; lost replies
must use the original key, and an expired lease cannot be reacquired as fresh work.

Acquisition and renewal, including idempotent replay, recheck live credential
lineage, grant creator, project scope/limits, task basis, input integrity/release,
epoch and expiry. Cached authority is rejected once stale or revoked. Epochs
advance on admission ownership changes and revocation; renewal of the same lease
preserves its epoch. This lets the supervisor retain a stable operation identity
while extending only the permitted expiry.

`POST /worker/credential-access-leases/{id}/access` is worker-audience only. It
derives the worker from the authenticated workload credential and rechecks tenant,
project, task, grant status/expiry, execution lease, worker registration, and
epoch on every call. It returns only a short-lived opaque development handle, never
a reference or raw value. Credential-access lease renewal is human-only and fails
after access-lease revocation, parent expiry/cancellation, worker retirement, or
epoch fencing. The private test fake backend rechecks those bindings again when a
trusted local worker resolves its handle. Individual handle expiry or invalidation
affects only that handle; authoritative lease/grant/worker/epoch changes fence all
handles and future issuance. It is not a production secrets backend.

Observed states are `running`, `exited` and `unknown`. Observations are attributed
to the enrolled worker, with both observation and recording timestamps. They must
not predate the lease or prior observations; future skew is limited to five seconds.
A known provider ID cannot change during the lease. Confirmed exit requires a
provider ID. These are trusted-worker assertions, not independent provider proof
or scientific execution receipts.

Schema v18 creates one external-operation intent atomically before returning an
executable package. It freezes the authority binding, package/request digests,
adapter/profile, deterministic Docker lookup key and `manual_reconciliation`
retry class. Journaled observations must match that operation, lease, worker and
epoch; their first provider ID is immutable. Revocation changes desired state to
`cancel_requested` in the same transaction, without a provider call. v17
lease/observation rows remain unbackfilled/null; artifact, manifest and synthetic
checkpoint compatibility stays at the existing derived-operation boundary.

Revocation marks `cancel_requested`, advances the grant epoch and blocks new
actions. The previously assigned worker may still report observations for its
lease to account for an outstanding job, provided its workload credential remains
valid. Such reports cannot renew authority or commit scientific results.

Grant responses and the corresponding cancellation/observation labbook events
include a bounded `cancellation` projection. Its state is `requested` until an
observation arrives, `unresolved` for running, unknown, or unconfirmed exit, and
`confirmed_termination` only for that worker's exited observation with explicit
termination confirmation. It includes only the immutable request time and latest
attributed observation identity/facts. It is not a provider receipt or a stopped,
successful, quiescent, settled, or scientifically accepted result. Repeating a
cancellation returns the existing state and cannot clear uncertainty or create a
lease.

Reconciliation uses the current operation observation when an operation journal
exists: worker and receipt sources are ordered by observed time, with their
source-local cursors used only inside each source. A later `unknown` or `running`
observation blocks settlement even if an earlier source reported exit; a same-time
contradiction is also fail-closed. The human request's `provider_terminated` flag
cannot substitute for current combined confirmation. Legacy unjournaled leases
retain their worker-only reconciliation rule.
Unleased grants can reconcile only zero settlement because no lease was issued.
Expired or revoked grants retain exposure until reconciliation. Unresolved grants
also block task plan revision. A reconciled grant is an operational accounting
outcome; it never marks the task or a finding scientifically successful.

## Remaining integration gates

[Input delivery](input-delivery.md) now checks live authority and verifies bytes
through worker staging. Connect leases and frozen packages to the supervisor,
mount the verified inputs,
journal actual operation submission, and collect/quarantine bounded artifacts.
Then test real container cancellation, crash recovery, control-plane disconnects,
credential expiry and restore fencing. Worker trust/build registration, secret
brokering beyond the in-memory fake handle seam, real provider credential
integration, and invoice/usage reconciliation also remain incomplete. Do not treat
these control-record tests as end-to-end execution acceptance.

Future Kubernetes admission must additionally bind an explicit target identity and
an immutable human-approved execution plan; no ambient kube context is admissible.
Provisioning is a separate scoped-operator workflow, not an effect of execution
admission. Planned credential delivery (local environment source or Kubernetes
Secret materialization) requires its own explicit authorization and is not
implemented by schema v21's metadata-only lease.

## Synthetic result checkpoints (schema v15)

`POST /execution-grants/{id}/result-checkpoint` is a human-only, idempotent
control command. It records one immutable `committed_quarantined` checkpoint for
an exact reconciled grant/lease/derived operation and an existing quarantined
manifest. The request supplies the current reconciled grant revision and epoch,
lease and operation identities, manifest ID/digest, and the complete declared
artifact metadata/total under `synthetic-declared-output-v1`. Reusing a command
key with changed content conflicts; an equivalent commit with another key returns
the original immutable checkpoint. `GET /projects/{id}/result-checkpoints` lists
these records under normal project scope.

The commit transaction rechecks current grant/lease epoch, latest confirmed
`exited` worker observation, reconciliation, manifest ownership/identity,
non-late quarantine state, exact approved output paths, retained BLOB type/size
and SHA-256 for every entry. It writes the validation record, audit event and
idempotency response atomically. Stale, incomplete, corrupt, late, foreign, or
changed validation inputs are rejected; neither the manifest nor its bytes are
released or relabeled.

This is deterministic synthetic validation of declared bytes and metadata only.
The stored record explicitly says `scientific_validity=not_assessed` and
`execution_receipt=trusted_worker_termination_observation_only`: it does not
attest provider success, settle previously unresolved exposure, accept a finding,
or enable runtime collection/provider dispatch.

## Bounded human result bookkeeping (schema v20)

`POST /result-checkpoints/{id}/acceptances` appends one human-only `accept`,
`reject`, or terminal `retract` record. The request has a nonblank rationale,
expected ledger revision, checkpoint/manifest/validation/receipt digests, and
the matching reconciled grant revision, lease, operation, and epoch. The first
decision uses revision 0; only a current accept/reject can be retracted, and a
retracted ledger cannot be reopened. `GET /result-checkpoints/{id}/acceptances`
returns bounded immutable history under the same tenant/project scope.

The transaction rechecks live human credentials, reconciliation, exact receipt
identity and confirmed fake-provider exit, non-late quarantined/unclassified
manifest state, complete declared paths, and every retained BLOB's type, size and
digest. Stale, missing, foreign, mutated, incomplete, released, or changed inputs
fail closed. Idempotent same-content replay returns its original record; changed
content conflicts. v19 migration creates no records.

This is only Tessera authoritative bookkeeping/ownership of development-synthetic
evidence. Each record permanently labels `scientific_validity=not_assessed`,
`provider_success=not_established`, `artifact_release=not_performed`, and fake
provider/development evidence. It does not release/download/publish artifacts,
settle costs, accept/create findings, infer scientific truth, or change the
checkpoint, manifest, artifact, receipt, grant, or existing findings.
