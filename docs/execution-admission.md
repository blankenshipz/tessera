# Local CPU admission records

Schema v6 adds project execution limits, input release decisions, execution grants,
worker leases, observations and reconciliation. These routes commit authority and
accounting records. A separate [input delivery](input-delivery.md) route transfers
released bytes under a live lease. Container submission, harness startup and
artifact collection are not connected; the CPU supervisor remains separate.
No paid or real research jobs were used to validate this slice.

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
| `POST /worker/observations` | lease_id, expected_epoch, observed_state, observed_at, optional provider_id, termination_confirmed |

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

Observed states are `running`, `exited` and `unknown`. Observations are attributed
to the enrolled worker, with both observation and recording timestamps. They must
not predate the lease or prior observations; future skew is limited to five seconds.
A known provider ID cannot change during the lease. Confirmed exit requires a
provider ID. These are trusted-worker assertions, not independent provider proof
or scientific execution receipts.

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

Reconciliation uses the latest worker observation. A later `unknown` or `running`
observation blocks settlement even if an earlier record reported exit. The human
request's `provider_terminated` flag cannot substitute for worker confirmation.
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
brokering and invoice/usage reconciliation also remain incomplete. Do not treat
these control-record tests as end-to-end execution acceptance.

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
