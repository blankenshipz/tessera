# Artifact quarantine

Schema v7 adds immutable artifact quarantine; v8 adds resumable upload sessions.
Schema v9 adds immutable complete output manifests; v10 adds human disposition history.
Schema v23 keeps quarantine as bounded intake but separates durable project
placement. After checksum/size validation, a durable intent precedes backend
stage/commit/reconciliation into private project storage before workspace cleanup. This does not
classify or release bytes, accept a finding/result, settle cost, or establish
provider/scientific success; historical SQLite BLOBs have no inferred placement.
Upgrade existing state explicitly
with `tessera migrate` after stopping the service and backing up its private state.
The migration preserves grants, leases, reservations and retained inputs; failure
rolls back the new table and schema version together.

Schema v19 fake-provider receipt evidence is separate from these artifact records.
It neither releases quarantined bytes nor makes a manifest, provider observation,
or synthetic output a result-acceptance or scientific-success record.

Schema v46 adds a separate human **custody release** for researcher retrieval.
It is available only after the current human disposition is
`retain_quarantined`, and it is an append-only `released`/`revoked` decision
fenced to the exact immutable manifest digest. A release permits a live,
project-scoped human credential to retrieve an exact private placed object,
read its released manifest, or request a bounded preview. It does not alter the
manifest's `quarantined`/`unclassified` status, classify or publish bytes,
accept a result, or release input data. Revocation fences subsequent manifest,
preview, byte, and stored-bundle reads.

`POST /artifact-manifests/{manifest_id}/reproduction-bundles` creates an
immutable, digest-addressed, 4 MiB-or-smaller private reproduction snapshot
from one released manifest. It includes verified released output bytes,
manifest and custody/disposition history, frozen task plan references,
operation-linked admitted-attempt outcomes (including failed, cancelled, and
unknown/inconclusive outcomes), and linked measurements. Every export runs in
one SQLite read snapshot and fails closed on missing, corrupt, deleted, or
unreleased output custody. Inputs remain separately released/unclassified and
are never copied into this export; the bundle declares them, skill material, and
environment-image material as restricted or missing rather than implying a
complete scientific reproduction.

Schema v20's human result-bookkeeping ledger may reference an exact checkpoint
only after it rechecks these bytes and their still-`quarantined`/
`unclassified`, complete, non-late manifest. That append-only record does not
change artifact bytes, status, classification, disposition, download or release.

`POST /worker/leases/{lease_id}/artifacts` requires a live worker credential and an
idempotency key. The strict JSON body contains `expected_epoch`, `operation_id`,
`relative_path`, `sha256` and `content_base64`. The operation ID must match the
lease's deterministic CPU operation identity. The path must be explicitly
declared in the historically approved plan and the supplied checksum must match
the decoded bytes. File content is limited to 16 MiB; cumulative stored bytes for
the lease cannot exceed the smaller of 16 MiB and the plan's output limit,
including bytes reserved for incomplete uploads.

For larger files, use the resumable worker endpoints with an idempotency key on
each request:

1. `POST /worker/leases/{lease_id}/artifact-uploads` begins an immutable upload
   with `expected_epoch`, `operation_id`, `relative_path`, `sha256` and `size`.
   It reserves the declared size against the same output budget as direct uploads.
2. `POST /worker/leases/{lease_id}/artifact-uploads/{id}/status` with `{}` returns
   current progress, including `received_bytes`. Restarting does not lose bytes.
3. `POST /worker/leases/{lease_id}/artifact-uploads/{id}/chunks` appends a nonempty
   chunk using `offset` and `content_base64`. The offset must equal committed
   progress, and decoded chunks cannot exceed 256 KiB.
4. `POST /worker/leases/{lease_id}/artifact-uploads/{id}/complete` with `{}` checks
   the complete size and checksum and atomically creates the quarantined artifact.

A lost response can be retried with the original key. A status read discovers
committed progress before resuming. Another key cannot replace a path or change
an upload's declared content. Incomplete uploads reserve their declared bytes;
there is no abort, replacement, or automatic cleanup in this slice. Completed
uploads retain metadata and release staging bytes. Completion remains quarantine,
including when the grant has expired or been revoked.

`tessera_worker.artifacts.upload_artifact` implements this protocol for retained
immutable bytes. It validates receipts and sends requests below the HTTP channel's
1 MiB bound. Transport failures return to the caller without automatic retry;
calling again with the same retained bytes resumes the transfer. The caller must
run transfers outside the supervisor cancellation loop and retain source bytes
until completion. This helper does not itself collect or persist container files.

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

`POST /worker/leases/{lease_id}/artifact-manifest` records a complete output set.
Its body contains `expected_epoch`, `operation_id`, and `artifacts`, each with
`id`, `relative_path`, `sha256`, and `size`. Entries must match exactly the
historically approved output paths and completed artifacts for that lease and
worker. Control checks stored BLOB type and length before reading bounded bytes,
then rechecks their hashes. Missing, foreign, or corrupted artifacts prevent
manifest creation.

One immutable manifest is retained per lease, with sorted entries and a version-1
digest over its frozen project/task/grant/lease/worker/epoch/operation/plan identity
and entries. A repeated command cannot replace it. Live credential and tenant
checks apply to replay as well as new commands. Expired or revoked execution
authority can produce attributable late quarantine, but never accepted results.
`GET /projects/{project_id}/artifact-manifests` provides scoped metadata pagination.

`tessera_worker.outputs.collect_outputs` provides a bounded reader for a caller-
verified stopped workspace. It checks declared paths through directory descriptors,
rejects links and special files, and returns immutable bytes only after the entire
collection succeeds. `upload_output_set` transfers those retained bytes and records
the manifest; retries resume the same uploads and manifest. Neither helper proves
provider termination, enforces a filesystem quota, or durably seals source bytes.
Automatic runtime collection remains disabled until the
[persistent output-storage gates](decisions/0003-persistent-output-storage.md) pass.

`tessera_worker.sealed_outputs.SealedOutputJournal` adds synthetic fixture-only
evidence for the missing local custody step. Under a private `0700` synthetic
runtime root it requires a fresh, matching control-acknowledged exited/termination
observation before calling `collect_outputs`, then atomically stores the immutable
bytes, digests, exact local seal manifest, and termination-observation identity in
SQLite. A restart resumes those sealed bytes through `upload_output_set`; a lost
upload or manifest response therefore retains the same operation and seal identity
without rereading a changed workspace. Recalling `seal` detects changed bytes or a
conflicting declaration. This local journal is not control authority and does not
make the worker observation more authoritative than its existing operational-only
meaning.

The journal is deliberately not wired to `CpuRuntime`'s post-exit path. In
particular, it does not activate a macOS Docker Desktop output backend, reserve or
reclaim a storage slot, collect real runtime output automatically, release
quarantine, accept a result, dispatch a provider, use credentials, or establish
scientific success. The existing service continues to reject undeclared manifest
paths and stale epochs, and retains attributable late bytes as quarantine only.

For explicit local development retention, `tessera_worker.development_storage`
provides `DevelopmentArtifactStorage` and a marked `SyntheticStoppedWorkspace`.
It uses a fixed `development-only-artifact-seals` child path and the journal
above, requires the same fresh termination evidence, and returns the existing
quarantined/unclassified manifest. This is fixture tooling only: its label is
not backend activation, package admission, runtime mounting, quarantine release,
result acceptance, or scientific evidence.

## Human manifest dispositions

Completed worker outputs remain quarantined after a manifest is admitted. A
human may append a disposition with
`POST /artifact-manifests/{manifest_id}/dispositions`, supplying
`expected_manifest_digest`, `expected_revision`, `disposition`
(`retain_quarantined` or `reject`), and a nonblank `rationale` of at most 8000
characters.

Dispositions are immutable history. The first disposition uses expected
revision `0`; each successful compare-and-set appends the next revision. A
`reject` records that the manifest is rejected for use while retaining the
evidence. `retain_quarantined` supersedes the review decision without releasing
the manifest. Neither choice changes artifact bytes, manifest status or
classification, execution history, accounting, release, download, deletion,
dispatch, scientific acceptance, or settlement.

`GET /artifact-manifests/{manifest_id}/dispositions` returns bounded metadata
pages ordered by revision. Both routes enforce organization and project scope;
only live human control credentials may write. Worker and agent credentials are
denied. Idempotent replays return the original historical disposition after a
newer revision is appended, provided the credential and manifest access remain
valid.

This slice does not collect files automatically from containers, perform scientific
result validation, publish evidence, or schedule retention and cleanup. A custody
release is private read access, not a quarantine release. The CPU runtime still needs a bounded output-storage contract: Docker
[removes tmpfs contents when a container stops](https://docs.docker.com/engine/storage/tmpfs/),
so the current `/work` mount cannot provide post-exit artifact collection.
Synthetic API/storage fixtures are not evidence of a
completed research run.

The opt-in Stage 1 native development worker is the only automatic caller of
the marked `DevelopmentCpuRuntime` collection seam. It reports a fresh confirmed
exit, seals declared files, transfers the existing manifest, and checks that the
receipt remains quarantined/unclassified before cleanup. It never releases a
manifest, commits a result checkpoint, or treats a container exit as provider
or scientific success. Any transfer or cleanup uncertainty leaves the local
workspace for explicit inspection/retry.
