# Large artifacts (model weights and other multi-gigabyte outputs)

A stage output is identified by one sha256 over the whole file, whatever its size.
Outputs up to 16 MiB keep the original single-BLOB quarantine path unchanged. Larger
outputs are stored as fixed 4 MiB chunks and moved with bounded memory: no component
holds more than about one chunk of a large file at a time, and nothing base64-encodes
or buffers the whole artifact.

## Limits (operator-configurable)

| Setting | Default | Range | Where |
| --- | --- | --- | --- |
| `TESSERA_MAX_ARTIFACT_BYTES` | 8 GiB | 16 MiB to 1 TiB | control environment, read once on first use (`operator_limits`) |
| `TESSERA_MAX_PROJECT_ARTIFACT_BYTES` | 32 GiB (at least the per-artifact limit) | up to 64 TiB | control environment |
| `TESSERA_MAX_REQUEST_BYTES` | 24 MiB | 512 KiB to 256 MiB | control environment: the largest HTTP request body control accepts. Set it to the smallest request size on the path to control (a reverse proxy in front may bound it lower); control then tells workers how large a part of a chunk fits |
| `TESSERA_ORPHAN_ARTIFACT_SECONDS` | 7 days | 1 hour to 365 days | control environment: how long a completed large artifact that no manifest lists is kept before it is reclaimed |
| `max_output_bytes` | the per-artifact default | 16 MiB to 1 TiB | `development-poll` config / `--max-output-bytes`: a worker host with little disk can cap itself below the control limit |
| `max_input_bytes` | the per-artifact default | | `DockerCpuAdapter`: the largest staged input a stage may mount |

A malformed or out-of-range value stops the process at start with a clear error. The
limits are ceilings on what an approved plan may ask for, not allocations. A plan whose
`max_output_bytes` exceeds the control limit is refused at submission (422). The limit is
also checked when a package is built for a stored plan, so a plan above a lowered limit
gets no new grant package (409) until it is revised; uploads for a lease that already holds
a package use the smaller of the plan's bound and the current limit, and a stored spec is
re-validated against the absolute 1 TiB ceiling only. The per-project limit counts large
artifacts and chunked snapshots together. Chunked-snapshot bounds, the provider output total
and the Salad input ceiling are snapshots of the same environment taken when their modules
load, so set the environment before starting the service.

A fresh install needs no setting: 8 GiB per artifact suits models of several GB. Size
the control host's disk for the project limit: **chunk bytes live in the control SQLite
database** (the same honest limit as chunked snapshots, ADR 0037), and composing a stage
input copies the chunks, so a consumed artifact is held about twice. **A composed snapshot
is never collected today**: no route deletes a completed snapshot, so its copy counts against
the project's chunk cap until the project's data is removed out of band. Object storage is
future work.

## Flow

1. **Worker upload.** For an output above 16 MiB the worker streams the file:
   `POST /worker/leases/{lease}/large-artifact-uploads` (declares path, size and the
   whole-file sha256; idempotent), `.../{id}/chunks/{seq}` (one chunk with its own
   sha256; naturally idempotent), `.../{id}/status` (resume from `next_seq`) and
   `.../{id}/complete`. The lease, grant, epoch, operation and declared-path fences are
   the legacy upload's. Chunks are bound to the upload by lease, worker and declaration.
   Control accepts this route only for files **above** 16 MiB: smaller files must use the
   inline path, which places them in the project object store, so a worker cannot use the
   chunked path to skip placement and retention. Chunks may arrive out of order; the
   worker sends every chunk, so a stored chunk that differs from its file is refused.
   **Request size.** A 4 MiB chunk is about 5.6 MB of base64 and JSON, more than a request may
   carry on some paths (the worker's control channel bounds a request at 1 MiB; a proxy may too).
   `begin` and `status` therefore advertise control's `max_request_bytes` and `part_bytes`, the
   largest raw part of a chunk whose base64 and envelope fit it with headroom (a multiple of
   64 KiB; the whole chunk with the 24 MiB default). The worker sends each chunk in parts of at
   most the smaller of that and what its own channel carries: `PUT .../chunks/{seq}` with
   `part_offset`, the same whole-chunk `sha256` in every part, parts in order from the start. A part
   whose bytes were already stored is a no-op; a gap, other bytes or another digest is refused.
   When the last part completes the chunk its digest is checked, and a mismatch drops the stored
   parts (committed) so the chunk can be sent again from the start. A chunk is *received* only when
   whole, so resume works as before, and the stored chunk rows, the plan and every later step
   (manifests, custody, composition) are unchanged. If no part fits the smallest bound on the path
   (the smallest part is 64 KiB), or control refuses a part with HTTP 413, the worker fails the
   upload at once with an error naming the numbers and `TESSERA_MAX_REQUEST_BYTES`; it does not
   retry. A control from before this change advertises nothing and takes whole chunks only.
   **Resuming after a restart.** `status` lists `partial_chunks`: each chunk whose parts have not
   all arrived, with the bytes control holds from its start. A restarted worker continues each
   such chunk at that byte, with whatever part size now fits, so a confirmed part is never sent
   again and parts stay in order; a chunk control holds whole is not sent again either (a chunk
   whose digest then fails is dropped by control, and the next attempt restarts it from byte 0).
   Control answers a part it already holds with what it holds, and the worker accepts that.
   An upload lives 24 hours (`expires_at` is in every receipt). An expired upload no
   longer reserves lease or project budget, and a new `begin` reclaims it and starts again;
   so does a `begin` under a newer lease epoch, which discards bytes sent under the old one.
2. **Verification.** `complete` re-hashes every chunk and then the whole artifact, one
   chunk at a time and outside the write lock, then binds the quarantine row to exactly
   the chunk set it hashed. A missing, wrong or altered chunk, or a sha256 that is not
   the declared one, refuses completion (409) and leaves nothing quarantined. Hashing several
   gigabytes takes longer than a worker's request deadline (at most 20 s), so `complete` waits
   only a few seconds (`VERIFY_WAIT_SECONDS`) and otherwise answers **202** `{"status":
   "verifying", "retry_after_seconds": N}`, not recorded as the answer to the key. The
   verification keeps running, one per upload and chunk set; the worker asks again with the same
   key, a repeat joins that verification instead of hashing the file again, and the call that
   finds it done gets the ordinary 201 receipt (a replay after that is the recorded answer). A
   worker waits at most 9 minutes of elapsed time, counting the time each ask itself takes, and
   then gives up and retries later; that is below the 10-minute stall window of the exited-attempt
   hold (ADR 0064), so a worker never keeps waiting for a verification the hold has stopped
   waiting for (a verification slower than that ends as an incomplete transfer, as before). At
   most four whole-file hashes run at once on control, and a finished answer is kept for an hour.
   A worker that does not know "verifying" rejects the 202 body (it carries no lease or epoch),
   fails that attempt, and its next ask gets the 201 without another hash.
3. **Quarantine.** The result is the ordinary quarantined, unclassified `artifacts` row
   (same id scheme, events and receipt shape); its `content` is empty and its bytes are
   the chunk rows. Manifests list it like any artifact. It has no project-object-store
   placement or retention record (like a legacy unplaced artifact) and cannot be
   previewed, retrieved inline or put in a reproduction bundle: those refuse with a clear
   error.
4. **Custody to input.** After the usual human (or campaign-approved) retain
   disposition and custody release of the manifest, composition copies the chunks into
   a chunked snapshot, re-hashing each chunk and then the whole file, with bounded
   memory. The copy runs **before** the write transaction, in short batches (a few chunks
   per transaction) into a staging upload of the new snapshot, so control writes are not
   blocked for the duration of a multi-gigabyte copy. The command then re-authorizes the
   request and binds to exactly what was copied (manifest, live staging upload, source
   chunk fingerprints) before creating the snapshot, and inline sources are loaded one at
   a time. A failed compose deletes its staging copy at once; an abandoned one expires
   with the staging upload. The composed snapshot is `private_derived`, exactly as for
   small outputs.
5. **Consumption.** Providers take the snapshot as a stage input through the existing
   chunked delivery: local Docker and self-hosted workers materialize it with streaming
   verification; Salad workloads fetch it with a provider input ticket. Lineage edges,
   the campaign stage I/O index and the receipt record the artifact's sha256 and size as
   for any output.

## Salad stages

A Salad stage consumes a large artifact through the existing guardian-held input path
(ADR 0036 phase 2): the composed chunked snapshot is pulled with a provider input ticket
and served to the workload in 1 MiB ranges of verified 4 MiB chunks. The reviewed contract's
`input.max_bytes`, the guardian, the worker's chunked-input verifier and the GPU runtime
all follow the operator artifact bound (default 8 GiB), where the ceiling was a fixed
2 GiB. A multi-GB input is thousands of range requests (about 1,024 per GiB) within one ticket's
lifetime, so practical sizes depend on that lifetime and the guardian's serve budget.
`tests/test_large_input_bounds.py` drives the guardian client against a fake control serving
a virtual 3 GiB artifact (first, middle and last chunks, in ranges); no live Salad run has been
made, and the GPU image must be rebuilt and re-pinned to pick up the new mirror constant.

## Reclamation

Chunk bytes are freed in short batches (never one long write lock), their rows leave the
project's chunk-cap accounting, the quarantine row stays as evidence, and every reclaim
appends a `control.large_artifact_reclaimed` event (artifact id, path, size, reason):

- **Rejected manifest.** Rejecting a manifest frees its large artifacts after the decision
  commits, and a later `retain_quarantined` for it is refused ("reclaimed after rejection").
- **Unmanifested orphan.** A completed large artifact that no manifest lists (a failed task,
  or a lease abandoned after `complete`) is reclaimed once **its execution is over** (its
  lease is past its hard expiry and its grant is reconciled or expired) **and** it is older
  than the operator's orphan age (`TESSERA_ORPHAN_ARTIFACT_SECONDS`, default 7 days). An
  artifact whose lease may still post its manifest is never reclaimed, whatever its age, so
  the age may safely be shorter than a long task; it only needs to leave a person time to
  look.
- **Sweep.** Both cases run as a sweep at a large-upload begin for the project (bounded to
  two artifacts per begin and 32 plausible candidates examined, oldest first; the candidate
  query already excludes artifacts listed by a manifest that is not rejected and artifacts whose
  lease is live, so those cannot starve newer orphans; skipped on an idempotent replay; its
  failures never block the upload), and a human can run it with
  `POST /projects/{project_id}/large-artifacts/reclaim` (Idempotency-Key required; at most 64
  artifacts per call, 512 candidates examined, repeat for more). A key that already has an
  answer returns it without sweeping again; only a new key reclaims. Same-key calls in one
  control process are serialized by one of 64 locks chosen from (principal, key), so unrelated
  keys and projects rarely wait on each other; two control processes racing on one fresh key
  may each reclaim part of the set, and the recorded answer is the first writer's. The sweep also
  finishes a rejection whose reclaim never ran because the process stopped after the decision
  committed.
- **Atomic.** The scan is only a candidate list. The first write transaction of each
  artifact re-checks, under the write lock, that it still has no manifest, no live lease and
  no non-reject decision, and deletes its first batch in that same transaction; a manifest or
  decision that commits after the scan wins and nothing is deleted. Once bytes start to go,
  a manifest can no longer commit (its chunk check fails) and a retain is refused.
- **Restore hold.** While control is held after a restore, nothing is reclaimed: every batch
  transaction checks the recovery state first and a human request is refused (403). A restored
  database can lack manifests that committed later, so artifacts in use would look orphaned.
  A hold that starts mid-reclaim stops it at the next batch; the sweep finishes it afterwards.
- **One event.** A reclaim is claimed by deleting the artifact's completed upload record,
  which exactly one of any number of interleaved calls can do; that call alone appends the
  event, so concurrent sweeps, replays and repeated rejects record each reclaim once. It carries the artifact id,
  path, size, reason and trigger (`human`, `worker_begin` or `reject`); it is attributed to
  the principal that triggered it, which for a worker-begin sweep may belong to another task
  of the same project.

An artifact in a manifest that is still awaiting a decision is never swept, however old: a
human can retain or reject it. In-flight uploads are not artifacts yet; they are handled by
upload expiry. A retained artifact's chunks are kept, and a composed
snapshot holds its own copy. Expired, unfinished uploads are collected as before; that
collection deletes a whole expired upload in one transaction, a short lock spike rather than
a correctness problem.

## Limits that remain

- A Salad stage's own *outputs* keep the 16 MiB bound (a documented limit, not a design
  goal); only its inputs follow the operator bound. Consuming a large artifact also needs the
  ADR 0040 trust binding for `private_derived` data.
- A multi-GB Salad pull has not been run end to end; the guardian test streams selected chunks.
- The control SQLite database stores the bytes; there is no object storage, no resumable
  download for humans, and no inline retrieval of a large artifact.
- Verification at manifest, release and delivery time checks chunk metadata and each
  delivered chunk's hash, not a fresh whole-file hash of gigabytes on every call; a full
  re-hash happens at upload completion and while composing.
- Tests use tiny thresholds; no multi-gigabyte transfer has been run in the default suite.
