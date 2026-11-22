# 0037 — Chunked input snapshots and generic provider input delivery

## Status

> **Amended by [ADR 0050](0050-large-artifact-custody.md):** the 2 GiB per snapshot and
> 8 GiB per project bounds below are now the operator limits (defaults 8 GiB and 32 GiB).

Accepted (2026-11-18, owner direction). Implemented in `34cf22e` (chunked
snapshots and worker delivery) and `6b7f384` (provider input tickets), against the
fake provider only, with default tests. No live provider, Salad credential or
network path establishes it. Provider input delivery is a narrow, owner-approved
exception to ADR 0033 (freeze over-built planes; on main): generic delivery of
staged snapshot bytes to a GPU run. It adds no Salad code. The live session owns
the real Salad wiring and the merge.

## Relation to ADR 0036

This record implements phase 2 of
[ADR 0036, generic GPU workloads on Salad](0036-generic-salad-gpu-workloads.md)
("Input delivery (ADR 0034 phase 3)"). It does so generically and
provider-neutrally, against the fake provider. ADR 0036's bounds for that phase
hold: delivery is read-only, bound to the staged snapshot's digest and size, and
available only to the bound workload of one operation.

- **What this record supplies.** The snapshot side of that delivery:
  - chunked, checksummed snapshots large enough for real datasets;
  - a revocable, operation- and snapshot-scoped ticket, issued under the existing
    lease, grant and release fences;
  - the ticketed pull routes `/provider-inputs/manifest` and
    `/provider-inputs/chunks/{seq}`;
  - a stdlib-only verifying client, `tessera_worker.provider_inputs.fetch_snapshot`,
    which pins the plan's digest.
- **What ADR 0036 still owns.** The Salad-specific half, which the live session
  wires:
  - the guardian's `/v1/workload/input` endpoint and its IMDS workload-identity
    check;
  - either passing a ticket into the closed Salad environment, or having the
    guardian hold the ticket and stream verified chunks to the bound workload;
  - the guardian's check that the delivered digest equals `manifests.data`.

  Neither path needs a change to the contract defined here.

## Context

Experiments run only inside Tessera (owner rule). Data an agent discovers is
staged as an input snapshot ([0034](0034-governed-sandbox-egress-and-dataset-staging.md)).
Two limits stopped that data reaching a training run:

- **Size.** Snapshots are capped at 16 MiB and 1,000 files and are delivered as one
  base64 JSON body. The worker channel caps responses at 25 MiB.
- **Remote runs.** Only the worker that holds the lease can fetch inputs. A
  provider-side GPU workload has no identity or path to receive bytes. Salad only
  pins `input_snapshot_digest`.

## Decision

### 1. Chunked snapshots, same identity and release gate

- **Same identity.** A chunked snapshot is a normal `source_snapshots` row, so the
  snapshot id, digest, human `ReleaseSnapshot`, grant and lease checks are unchanged.
- **Same digest.** The digest is the existing inventory digest, binding every
  file's path, size and sha256. Chunking never changes what a digest means.
- **Chunk storage.** Bytes live in `snapshot_chunks`, one row per fixed 4 MiB chunk
  of each file in manifest order. Each row has the file path, offset, size, sha256
  and content. Chunk hashes are integrity aids; file hashes and the digest are the
  binding.
- **Bounds.** 2 GiB total, 10,000 files and a 4 MiB manifest. Bytes are stored in
  the control SQLite database, which is an honest limit; object storage is future
  work. Legacy single-BLOB snapshots and their 16 MiB bounds are unchanged.
- **Human upload.** Large snapshots are created by a human through
  `POST /projects/{p}/chunked-snapshots` (begin with the expected digest and
  inventory), `PUT .../{upload_id}/chunks/{seq}`, and `POST .../{upload_id}/complete`.
  Complete re-verifies every chunk and file hash and the digest. Only then does it
  insert the snapshot row, with the event and idempotency record, in one
  transaction. Staging ([0034](0034-governed-sandbox-egress-and-dataset-staging.md))
  produces a chunked snapshot when the staged total exceeds 16 MiB.
- **Upload expiry and storage cap.** An incomplete upload expires 24 hours after it
  begins. Chunk puts and complete refuse an expired upload. Expired uploads and
  their chunk rows are garbage-collected when the next upload for the project
  begins, and by an explicit human `POST /projects/{p}/chunked-snapshots/gc`.
  Completed snapshots are never collected. Each project has a cap of 8 GiB of
  chunked bytes, counting completed chunked snapshots and unexpired uploads; begin
  refuses an upload that would exceed it.
- **Verification.** Authorization paths (release, grant, lease) check chunk-row
  metadata: the count, sizes, and that each file's chunks cover it exactly. They do
  not re-hash gigabytes on every call. Bytes are re-hashed per chunk at every
  delivery and end to end by the receiver, and a full re-hash is available as
  explicit verification. A mismatch at any point refuses delivery.

### 2. Worker delivery

`POST /worker/leases/{lease_id}/inputs` keeps its fences and receipt. For a chunked
snapshot, the response carries the manifest and a chunk index
(`seq, relative_path, offset, size, sha256`) instead of file contents, marked
`delivery: "chunked"`. The worker then calls
`POST /worker/leases/{lease_id}/inputs/chunks/{seq}` with the same lease, epoch,
grant and release fences on every request. `materialize_chunked_snapshot`
(stdlib-only) writes into a fresh private tree. It verifies each chunk, then each
file's sha256 and size, then the inventory digest, and removes the partial tree on
any failure. The private tree and the 0444/0555 permission rules are the same as
for legacy snapshots.

### 3. Provider input tickets (0033 exception)

A provider workload is not a Tessera principal. It receives a **provider input
ticket**:

- **Issue.** The lease-holding worker calls
  `POST /worker/external-operations/{operation_id}/input-tickets` with `lease_id`
  and `expected_epoch`. The fences are the same as input delivery: current lease
  and epoch, active grant, and an active, unexpired, public worker-transfer release
  of the granted snapshot.
- **Token.** Control returns a random token once and stores only its sha256. The
  token is bound to that operation, snapshot id and digest, lease and epoch. Its
  expiry is the earliest of the lease expiry, the release expiry and one hour.
  Issuance emits `execution.provider_input_ticket_issued`.
- **No idempotent replay.** Issuance is deliberately not idempotent. Only the
  token's sha256 is stored, and an idempotent replay would have to persist the
  plaintext token in the idempotency record. Every issue call instead mints a new
  token and revokes any earlier active ticket for that operation, in the same
  transaction. A worker that loses a response retries and receives a fresh token.
  The lost one is already revoked.
- **Pull.** `GET /provider-inputs/manifest` and `GET /provider-inputs/chunks/{seq}`
  authenticate only by `Authorization: Ticket <token>`. They are read-only. Every
  request re-checks the ticket's expiry and revocation, the lease epoch, grant
  status, release status and expiry, and that the operation is not cancelled or
  terminal. Any revoked link stops further pulls.
- **Revoke.** The worker can revoke a ticket explicitly, and revoking the release
  or grant revokes it implicitly. Served chunk counts are recorded on the ticket.
- **Provider side.** `tessera_worker.provider_inputs.fetch_snapshot` (stdlib-only,
  runnable inside a GPU image) pulls and verifies with the same chunked
  materializer. The expected digest is the one pinned in the approved plan, not a
  value the server supplies.
- **Fake provider.** It gains an optional input-staging step. The worker passes it
  the ticket and the control origin, and the fake workload materializes the
  snapshot. That proves the whole chain without a live provider.

Salad delivery (ADR 0036 phase 2): the ticket never enters the container. The trusted
server passes it to the guardian, which serves verified chunks at
`/v1/workload/input` (see `docs/salad-safety-service.md`). Pulls now run in a deferred
read transaction with a per-ticket cached chunk index, and take the write lock only
for the `chunks_served` update; chunked staging enforces the per-project cap.

Originally not done here: how a real Salad container receives the ticket. Its environment is a
closed allowlist in live-session files. The live session adds a ticket variable and
a guardian check that the fetched digest equals `manifests.data`.

## Consequences

- Staged, human-released data of up to 2 GiB can reach a remote run under the
  existing release gate. Nothing becomes executable by being delivered.
- A ticket is a bearer secret for the lifetime of one operation. It is scoped
  read-only to one snapshot, expires in at most an hour, is revocable and is never
  logged. Tessera sees which chunks were served, not what the workload did with
  them.
- One labelled migration adds the chunk, upload and ticket tables. The integrator
  renumbers it at merge.
