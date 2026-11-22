# 0050 — Large-artifact custody and operator artifact limits

## Status

Accepted and implemented (2026-11-21). Amends the bounds of
[ADR 0037](0037-chunked-inputs-and-provider-input-delivery.md) and the "Bounds" and
"Storage, honestly" statements and the "Not done" list of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md). The operating contract is
[large artifacts](../large-artifacts.md).

## Context

ADR 0040 capped every stage output at 16 MiB and left "chunked output custody above
16 MiB" undone, so a stage could not emit model weights, and a later stage (including a
Salad stage through ADR 0037's provider input path) could not take them as an input.
Chunked input snapshots had a fixed 2 GiB / 8 GiB-per-project bound.

## Decision

1. **One identity.** An output of any size is identified by one sha256 over the whole file.
   Outputs up to 16 MiB keep the single-BLOB quarantine path unchanged.
2. **Chunked quarantine.** Larger outputs go through worker routes
   (`large-artifact-uploads`: begin, status, chunk, complete) with the legacy lease, grant,
   epoch, operation and declared-path fences. Bytes are 4 MiB chunk rows in the existing
   chunk store (ADR 0037), keyed by the artifact id. Control re-hashes every chunk and then
   the whole file outside the write lock and binds completion to the hashed chunk set. The
   result is an ordinary quarantined artifact with an empty `content` and its real size.
3. **No placement bypass.** The chunked route accepts only files above 16 MiB; large
   artifacts have no project-object-store placement and cannot be previewed, retrieved
   inline or bundled.
4. **Custody to input.** After the usual retain disposition and custody release, composition
   copies chunks into a chunked snapshot before the write transaction, in short batches into
   a staging upload, then binds to the copy by chunk fingerprints. Consumption is the
   existing chunked delivery, including the guardian input path for Salad stages.
5. **Operator limits.** `TESSERA_MAX_ARTIFACT_BYTES` (default 8 GiB, 1 TiB ceiling),
   `TESSERA_MAX_PROJECT_ARTIFACT_BYTES` (default 32 GiB) and
   `TESSERA_ORPHAN_ARTIFACT_SECONDS` (default 7 days), read once through
   `operator_limits`. The operator limit applies at plan intake and package build; a stored
   spec is re-validated against the absolute ceiling only. The worker's `max_output_bytes`
   and the Docker adapter's `max_input_bytes` bound a host below the control limit.
5a. **Request size and parts.** A 4 MiB chunk is larger than a request some paths allow (the
   worker's control channel bounds a request at 1 MiB), which stalled uploads that no retry could
   fix. The storage chunk stays 4 MiB, because manifests, custody, composition and delivery all
   hold the same plan. `TESSERA_MAX_REQUEST_BYTES` (default 24 MiB, 512 KiB to 256 MiB) is the
   operator's request limit for control, and `begin`/`status` advertise it with `part_bytes`, the
   largest part of a chunk that fits it with envelope headroom. The worker sends a chunk as ordered
   parts no larger than the smaller of that and its own channel bound (`part_offset`, one whole-chunk
   digest). Control assembles them, counts a chunk as received only when whole, verifies the whole
   chunk digest at the last part (a mismatch drops the stored parts, committed), and treats a
   repeated part as a no-op. A mismatch of limits fails fast: no part fits, or a 413, ends the upload
   with an error naming the numbers, never a retry loop. A control without parts is used as before.
5b. **Resume after a restart.** A worker that restarted between parts used to resend the chunk from
   byte 0, and control's honest receipt (what it held, more than the part just sent) made the worker
   refuse the upload on every retry. `status` and an idempotent `begin` now list `partial_chunks`
   (`seq`, `received_bytes`); the worker continues each partial chunk at that byte, accepts a receipt
   that reports at least the bytes it sent, and never resends a confirmed part. Ordering and the
   request-size guarantees of 5a are unchanged.
6. **Reclamation.** Rejecting a manifest, and a sweep for artifacts no manifest lists, free
   chunk bytes in short batches and record `control.large_artifact_reclaimed` once per
   reclaim. An unmanifested artifact is reclaimed only after its lease is over and it is older
   than the orphan age; the first write transaction re-checks eligibility atomically, so a
   manifest or decision committed after the scan wins. A reclaim is claimed by deleting the
   artifact's completed upload record, so interleaved sweeps, replays and repeated rejects
   record it once, and nothing is reclaimed while control is held after a restore (a restored
   database can lack later manifests). The begin-time sweep is bounded and examines only
   plausible candidates, so retained or live artifacts cannot starve it; the human route
   (Idempotency-Key required) returns a recorded answer without sweeping again. Expired or
   epoch-stale uploads are restarted.

### Completion is single-flight and may answer 202

A live run of a 1,430-chunk (about 5.7 GB) output showed the weakness of verifying inside the
request: `complete` re-hashed the whole file in every call, the worker's request deadline (the
lease minus a renewal interval, at most 20 s) passed first, and each retry started another
multi-gigabyte hash while the first was still running, so the worker never saw an answer
and the attempt closed as an unresolved exit. `complete` now waits a bounded few seconds and
otherwise answers 202 "verifying" (never recorded under the idempotency key); the verification
runs once per upload and chunk set and later calls join it. The worker repeats the same request
until the receipt arrives, for at most 9 minutes of elapsed time (measured on the clock, so the
time each ask takes counts). That bound is deliberately below the 10-minute stall window of
ADR 0064's exited-attempt hold, so the worker never polls for a verification the hold has
stopped waiting for; a verification slower than that ends as an incomplete transfer, retried
later, as before. Control runs at most four whole-file hashes at once, keeps a finished answer
an hour (it is guarded by the chunk fingerprint, re-checked in the completing transaction) and
drops one only if it is still the flight under its key. A worker that predates the 202 rejects
its body and fails that attempt; its next ask gets the 201 with no second hash. The
in-transaction check that the stored chunk set equals the verified one is unchanged.

## Consequences

- Bytes remain in the control SQLite database (the honest limit of ADR 0037); a consumed
  artifact is held about twice, and composed snapshots are not collected. Object storage is
  future work.
- Salad stage outputs stay at 16 MiB. A Salad stage consumes a large artifact only through
  the guardian input path, tested against a fake control; no live pull of that size has run,
  and the GPU runtime image's mirrored constant must be rebuilt and re-pinned.
- Verification at manifest, release and delivery checks chunk metadata and each delivered
  chunk's hash; the full re-hash happens at upload completion and while composing.
