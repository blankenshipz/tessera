# Authorized worker input delivery

`POST /worker/leases/{lease_id}/inputs` accepts `{"expected_epoch": 1}` with a
worker-audience bearer credential and `Idempotency-Key`. The assigned worker needs
a current lease, active grant, unchanged approved plan and active public-input
release. The service checks issuer/worker eligibility, project scope, authority
epoch, expiry and retained input integrity in the same transaction as the audit
event and replay receipt. It checks lease expiry again after assembling bytes.

The response contains `receipt` and `snapshot`. The snapshot includes its ID,
pinned digest, original manifest (including source-root metadata and exclusions),
and files with `relative_path` and `content_base64`. Existing bounds limit inputs
to 16 MiB and 1,000 files, with a 1 MiB manifest; base64 increases transport size.
Original provenance/classification labels remain unchanged. The separate human
public release authorizes this destination; it does not prove source accuracy.

The receipt records authorized intent, with `delivery_confirmed: false`. The
event and idempotency record contain metadata only. Retrying rebuilds bytes after
fresh authority and integrity checks, reusing the original receipt without a
duplicate event. Revocation blocks subsequent requests, including cached-key
replays; it cannot retract bytes already sent or prevent delivery of a response
authorized immediately before revocation.

`tessera_worker.inputs.materialize_snapshot` verifies the expected snapshot digest,
lease ID, epoch and worker ID supplied independently by its trusted caller. It
checks canonical manifest integrity, exact file membership, sizes and SHA-256
checksums before creating a fresh private directory. Absolute/traversal paths,
duplicates, file/directory collisions, case aliases and Unicode normalization
aliases are rejected. It does not extract archives or execute files. Files are
read-only (`0444`) and have no executable permission; snapshot directories are
traversable (`0555`) under a private supervisor parent (`0700`).
The trusted owner can still change these permissions, so they are not a sandbox.

The caller must use an authenticated control channel and a private POSIX staging
parent under supervisor custody. Failed staging removes its new tree and preserves
existing files. No retained source root is resolved on the worker and no existing
directory is merged or overwritten. Downloaded bytes are not execution authority.

Synthetic tests cover HTTP delivery through local materialization, replay,
revocation, expiry, corruption, wrong audiences/workers, stale epochs and audit
rollback. [CPU runtime integration](cpu-runtime.md) connects these components to
the supervisor and verified read-only mounts. Native Docker isolation and revocation
fixtures pass. Pinned skill loading, authoritative receipt acknowledgement, runtime artifact collection,
retention/cleanup scheduling and end-to-end research execution remain incomplete.

## Staging collected outputs as inputs

A human can turn released run outputs into a local input snapshot with
`POST /projects/{project_id}/artifact-snapshots` (`Idempotency-Key`), body
`{manifest_id, expected_manifest_digest, relative_paths?, expected_snapshot_digest}`.
`relative_paths` selects a subset (default: every manifest file). The snapshot digest
fence is `inventory_digest` of an inventory with root `artifact-manifest:{manifest_id}`
and the selected entries sorted by path (`dataset_staging.staged_inventory`).

Gates, all in one transaction: human principal (agents and workers get 403); project
and tenant match; the manifest belongs to the project and its digest matches; the
current disposition is `retain_quarantined` and an active custody release names that
exact manifest digest (otherwise 409); every selected file is re-read from private
placement and re-hashed against the manifest SHA-256 (otherwise 409). The existing
snapshot bounds apply (16 MiB, 1,000 files, 1 MiB manifest); provenance stays
`imported_unverified`, classification `unclassified`.

The snapshot rows, `workspace.snapshot_adopted`, and the lineage event
`snapshot.staged_from_artifacts` (`snapshot_id`, `snapshot_digest`, `manifest_id`,
`manifest_digest`, `operation_id`, `files[{artifact_id, relative_path, sha256, size}]`)
commit with the idempotency record. `GET /snapshots/{snapshot_id}/staging` returns that
record (404 if the snapshot was not staged). Agents read it as the `inputs` section of
the experiment result summary (`results:read`).

Not done: staging does not authorize worker transfer; a human still issues the separate
`ReleaseSnapshot`, and a later plan names the digest in `input_snapshot_digest`. There
are no typed lineage edges until artifact lineage covers staged inputs, and Salad receives no bytes.

## Stage outputs inside one campaign (ADR 0140)

A campaign stage that reads an earlier stage's declared outputs (`kind: stage`) does not need
a human staging step: when the stage starts, the runner binds each output to the digest and
size its producing attempt recorded (the attempt must have succeeded and settled, and the
custody manifest must agree), re-checks the provider's trust for the resolved data class and
licence labels, and composes the input snapshot from exactly those digests. Any mismatch fails
closed with a closed refusal code, and the dependent stage's receipt lists the binding under
`input.late_bound_inputs`. See [campaigns](campaigns.md).

## Chunked snapshots (ADR 0037)

Chunk bounds are operator limits (default 8 GiB per snapshot and 32 GiB per project, see
[large artifacts](large-artifacts.md)); the ADR text below describes the original 2 GiB and
8 GiB defaults.

Snapshots above the legacy 16 MiB / 1,000-file bounds are stored as fixed 4 MiB chunks
in the control SQLite database. A chunked snapshot is an ordinary `source_snapshots`
row: same id, same inventory digest, same human `ReleaseSnapshot`, grant and lease
checks. Legacy snapshots and their bounds are unchanged.

Bounds: the operator artifact bound in total (default 8 GiB; it was a fixed 2 GiB), 10,000 files,
4 MiB manifest, 4 MiB per chunk. Bytes live in the
control database, which is the honest storage limit; object storage is future work.

Human upload (human principals only; agents get 403, workers 401):

- `POST /projects/{p}/chunked-snapshots` (Idempotency-Key): body `expected_digest` and
  `inventory` (`root`, `entries`, `exclusions`, `total_bytes`). Validated with the same
  rules as local adoption (safe paths, `imported_unverified`, `unclassified`, inventory
  digest equals the expected digest). Optional `data_class`
  (`{"data_class": "public", "license": ID, "source": URL_OR_REF}`; see
  [0039](decisions/0039-data-class-guard.md)): public needs both pieces of evidence and a
  labelled human decision, and without a declaration the snapshot is `private_derived`.
  Optional `code_approval` (`{"paths": [...]}`; ADR 0039 amendment 1): exact code or
  config files a human decision actor approves as private code for third-party
  compute; recorded with each file's sha256 and never relabelled public.
  Returns `upload_id` and `chunk_plan`
  (`seq, relative_path, offset, size`, manifest order, zero-size files have no chunks).
- `PUT /projects/{p}/chunked-snapshots/{upload_id}/chunks/{seq}`: body `content_base64`
  and `sha256`. Size must equal the plan and the hash must match. Re-putting identical
  bytes succeeds; different bytes are 409. Chunk bytes are never written to the
  idempotency table or events.
- `POST .../{upload_id}/complete` (Idempotency-Key): requires every chunk, re-hashes each
  chunk and each file (streamed across its chunks), then in one transaction inserts the
  snapshot row (id equals the upload id), marks the upload complete and records one
  `workspace.snapshot_adopted` event with `chunked: true`. Its response is metadata only
  and includes the recorded `data_class`. An optional body `{"data_class": ...}` may
  declare the class here instead; it must equal any declaration made at begin.

Staging ([0034](decisions/0034-governed-sandbox-egress-and-dataset-staging.md)) produces
a chunked snapshot when the staged total exceeds 16 MiB or 1,000 files, instead of failing.
A snapshot staged from run outputs is always `private_derived`; its request accepts no
`data_class` field.

Worker delivery: `POST /worker/leases/{lease_id}/inputs` returns the same receipt and,
for a chunked snapshot, `snapshot: {id, digest, manifest, delivery: "chunked", chunks}`
where `chunks` is the index (`seq, relative_path, offset, size, sha256`) and there is no
`files`. The worker then calls `POST /worker/leases/{lease_id}/inputs/chunks/{seq}` with
`{"expected_epoch": n}`, under the same worker audience, lease, epoch, grant and release
fences. It returns `snapshot_id, seq, relative_path, offset, size, sha256,
content_base64`. This is a read: no idempotency record, no events, and an
`Idempotency-Key` header is accepted and ignored.

Verification model: authorization paths (release, grant, lease, bundle index) check
chunk-row metadata only: the rows must equal the chunk plan of the manifest, each chunk
has its stored length and a sha256, and no legacy file rows exist. They do not re-hash
gigabytes. Each chunk is re-hashed whenever it is delivered (mismatch is 409), the
receiver verifies files and the digest end to end, and the explicit human verify
(`verify_snapshot`) fully re-hashes every chunk and file.

Upload lifetime and storage cap: an upload expires 24 hours after begin; PUT chunk and
complete refuse an expired upload (409). Expired unfinished uploads and their chunk rows
are deleted opportunistically inside each begin for that project, or explicitly by a
human with `POST /projects/{p}/chunked-snapshots/gc` (Idempotency-Key; the response is
counts only). Completed snapshots are never collected. Begin is refused (409) when the
project's completed chunked snapshots plus its unexpired uploads, plus the new upload's
declared `total_bytes`, would exceed the operator project limit (default 32 GiB; it was 8 GiB).

## Data class and tickets

A snapshot release carries a classification, `public` or `private_derived` (ADR 0038).
Only a `local-cpu` grant without `egress` may use a `private_derived` release; grant
issue, lease and egress plans refuse it otherwise.

## Provider input tickets (ADR 0037 section 3)

Provider input tickets refuse `private_derived` releases: they require `public`, so
private-derived bytes never reach a third-party provider.

A provider workload is not a Tessera principal. The lease-holding worker obtains a
short-lived **ticket** and hands it to the workload, which pulls the already released
snapshot itself. No ticket widens a release: it can only read the snapshot the grant
names, for the worker the release names.

Routes (`tessera_control/provider_inputs*.py`):

- `POST /worker/external-operations/{operation_id}/input-tickets`, worker audience,
  body `{lease_id, expected_epoch}`. In one write transaction: the operation belongs to
  this worker and `lease_id`; the lease epoch is current and unexpired; the grant is
  active; the snapshot release still authorizes the grant's snapshot for this worker;
  the operation is neither `cancel_requested` nor observed `exited`. Earlier active
  tickets for the operation are revoked and a new one is inserted. The response is
  `{token, expires_at, snapshot_id, snapshot_digest, manifest_path, chunk_path_template}`.
  Expiry is the earliest of the lease expiry, the release expiry and one hour.
- `POST .../input-tickets/revoke`, same body and ownership fences: revokes the
  operation's active tickets.
- `GET /provider-inputs/manifest` and `GET /provider-inputs/chunks/{seq}` carry only
  `Authorization: Ticket <token>` (no principal). The manifest response is
  `{snapshot_id, digest, manifest, delivery: "chunked", chunks}`; a chunk response is
  `{snapshot_id, seq, relative_path, offset, size, sha256, content_base64}`. A legacy
  (non-chunked) snapshot is served through a virtual chunk plan over the same rules, so
  any released snapshot is deliverable; the file hash is verified before slicing.

Secrecy: issuance is **not idempotent** and does not go through the idempotency layer.
Only the sha256 of the token is stored; the plaintext appears in no idempotency row,
event, log or ticket row. A worker that loses the response asks again: the old token is
revoked in the same transaction. The event `execution.provider_input_ticket_issued`
carries operation, snapshot, expiry and the count of revoked tickets, never the token.

Every pull re-checks, in its own transaction, that the ticket exists by hash, is not
revoked or expired, that the operation still has the ticket's lease and epoch, that the
lease is current, the grant active, the release still authorizes the ticket's snapshot
for the ticket's worker, and the operation is not cancelled or exited. Any such failure
is the same `401 {"detail":"Provider input ticket is not valid."}` (no oracle); a chunk
that fails its stored hash is `409`. Successful chunk reads update `chunks_served` and
`last_served_at` on the ticket; they write no idempotency rows and no per-chunk events.

Provider side: `tessera_worker.provider_inputs.fetch_snapshot` (stdlib-only) pulls with
no proxies and no redirects, requires HTTPS except on loopback, bounds each read, and
passes the result to the chunked materializer with the digest pinned in the approved
plan (the server's digest is checked against it before any chunk is fetched).
`python -m tessera_worker.provider_inputs --origin URL --token-file PATH --parent DIR
--expected-digest HEX` is the in-image entry point; the token file must be a private
0600 regular file. `ProviderDispatch` requests a ticket and calls the optional
`InputStagingProvider.stage_inputs` before submit when it is given an `input_origin`;
its journal records only milestones and the digest.

Not implemented here: a real Salad container's environment is a closed allowlist in
live-session files. That session must add a ticket variable (or token-file mount) and a
guardian check that the fetched digest equals the plan's `manifests.data` digest.
