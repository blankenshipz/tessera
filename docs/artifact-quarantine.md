# Artifact quarantine

Schema v7 adds immutable artifact quarantine. Upgrade existing state explicitly
with `tessera migrate` after stopping the service and backing up its private state.
The migration preserves grants, leases, reservations and retained inputs; failure
rolls back the new table and schema version together.

`POST /worker/leases/{lease_id}/artifacts` requires a live worker credential and an
idempotency key. The strict JSON body contains `expected_epoch`, `operation_id`,
`relative_path`, `sha256` and `content_base64`. The operation ID must match the
lease's deterministic CPU operation identity. The path must be explicitly
declared in the historically approved plan and the supplied checksum must match
the decoded bytes. File content is limited to 16 MiB; cumulative stored bytes for
the lease cannot exceed the smaller of 16 MiB and the plan's output limit.

Only the assigned worker can upload. Bytes, metadata, event and replay record
commit atomically. Replaying the same command returns the original metadata;
using a new key cannot replace an already recorded path. Content stays in the
artifact table and is excluded from events, replay records and API responses.

All outputs remain `quarantined` and `unclassified`. An upload never marks a task
successful, accepts a scientific finding, confirms provider termination or settles
cost. Expired/revoked execution authority may submit attributable late output only
while the worker credential remains live and scoped. Lease expiry, grant expiry,
epoch drift and current task-basis changes mark the record as late. Revoked worker
credentials cannot use even a previously successful replay key.

`GET /projects/{project_id}/artifacts` lists metadata for a scoped control
credential, using `offset` and `limit` (1–200). The query excludes BLOB content.
There is no raw artifact download or release endpoint yet.

This slice does not collect files from containers, scan or validate results,
acknowledge complete output manifests, publish evidence, or schedule retention and
cleanup. The worker HTTP transport currently limits requests to 1 MiB; a bounded
chunked upload path is still needed for larger artifacts before wiring collection
into the CPU runtime. Synthetic API/storage fixtures are not evidence of a
completed research run.
