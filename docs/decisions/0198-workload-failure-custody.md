# 0198 — A workload reports its own failure, waits for the stop, and leaves partial outputs and a checkpoint in custody

## Status

Accepted. Narrows the `gpu_runtime_v1` workload contract and the guardian's workload endpoints (ADR 0036
phases 3 and 5). One additive endpoint, two reserved output names, no schema change and no migration.
Needs a rebuilt runtime image; an image built from this recipe passes the start gate before it is allowed.

## Context

Four defects shared one cause: the runtime treated "the entry did not finish cleanly" as "exit and say nothing".

1. A trainer cut at its wall clock, or crashing late in a run, uploaded zero bytes. Staging and upload ran only
   after the entry returned, staging was all-or-nothing, and the work tree was removed on every failure.
2. The entry's stdin, stdout and stderr were discarded, so a traceback was visible nowhere.
3. A failing runtime printed one status line and exited 1. Salad replaces an exited container, so the guardian
   next saw a changed allocation and the route was recorded as a provider move although the workload had
   failed.
4. After the last chunk the runtime exited at once and the guardian's stop followed on a later pass. If a poll
   listed the replacement first, the guardian marked the allocation uncertain and a fully uploaded run ended
   `failed`.

Salad gives a replaced instance an empty disk and may interrupt without warning, so durable state must live
with the guardian, not on the node.

## Decision

1. **Closed failure report.** `POST /v1/workload/failed` carries `{binding, reason, exit_code}` and nothing else.
   `reason` is one of `entry, entry_timeout, output, output_plan, custody, deadline, input, fetch, install,
   manifest, internal`; `exit_code` is the child's exit status (an integer in -255..255) or null when no child
   failed. It is accepted only after begin-once, with the same IMDS identity, binding and freshness checks as
   every other workload call. The guardian records the stop cause `workload_failed:<reason>` (and the exit
   code beside it), starts the stop at once, and the verdict is `failed` with that reason, never
   `allocation_changed`. The first stop decision of an operation wins: a report after another stop, or with a
   different reason, is refused and changes nothing; an identical replay is acknowledged. A second container's
   begin-once after a report stays refused (the claim is spent). Nothing a workload writes ends up in the
   cause: no message, path or traceback travels in this call.
2. **Never exit on its own.** After the last chunk (`outputs_complete`) or a confirmed failure report the runtime
   sleeps up to 120 s and lets the guardian's stop end the container (SIGTERM ends the wait early); only the
   bound returns exit 0. A failure the guardian was not told about (before the claim, or a report the guardian
   did not confirm) still exits 1 at once, as before. The wait sits inside the billed upload grace.
3. **Partial outputs.** When the entry exits non-zero or is cut off, the runtime stages and uploads each declared
   output that exists as a regular file within its cap (missing, non-regular and oversize ones are skipped,
   never fatal), inside `UPLOAD_GRACE_SECONDS`, then reports the failure. `output.json`, when declared, gains
   `partial: true`, `partial_reason`, `partial_exit_code` and `partial_skipped` (the names not uploaded). The
   work tree is removed only after the upload attempt. A partial upload can cover every declared file (the
   entry wrote them all and then failed), which would complete the outputs and start a stop with cause
   `output_retained`. So the runtime uploads every file but the last, reports the failure, and then, only if the
   guardian confirmed the report, uploads the last file (the diagnostic, always staged last; an unconfirmed report
   leaves the outputs incomplete so a lost report can never verify as a pass): the first stop decision is then `workload_failed`, and the
   guardian still takes that file if it arrives before its next poll (best effort; the stop itself is not
   delayed beyond that poll). A custody error during the partial upload never replaces the entry's reason.
   Retry and resume classification stay out of this ADR.
4. **Soft deadline.** The entry sees `TESSERA_ENTRY_SOFT_DEADLINE_SECONDS` and `TESSERA_ENTRY_HARD_DEADLINE_SECONDS`
   (seconds from its start). It gets SIGTERM at the soft one (20 s before the hard one, or halfway when less
   than 40 s remain) and SIGKILL at the hard one. An entry that exits after our SIGTERM, even with status 0, is a
   cut-off run (`entry_timeout`), never a success.
5. **Checkpoint custody.** A contract may declare the reserved output `checkpoint.bin` (one file, its own byte
   cap, counted in the 16 MiB and 16-file totals). The entry must write it atomically (write a temp file in the
   output directory, fsync, rename): the runtime opens only the exact name, as a regular file without following
   links, so it never reads a temp file. The runtime uploads the latest complete checkpoint through the
   existing custody channel (this host only) at most every 120 s while the entry runs, only when it changed,
   and once more at the end or on failure. `checkpoint.bin` is the one custody file a later upload
   supersedes; every other output stays write-once. Supersedes are bounded: the chunk-call budget of an
   operation grows by `MAX_CHECKPOINT_UPLOADS` (10) uploads of the checkpoint's cap, in custody and in the
   service's per-operation request charge (`output_call_budget`). Declaring a checkpoint requires declaring the
   diagnostic output too: the diagnostic is uploaded last, so a checkpoint uploaded while the entry runs can
   never complete the outputs and end the run early. The runtime stages the diagnostic last whatever order the
   contract declares, so a final checkpoint is always in custody before the outputs complete. A successful run must leave a checkpoint when one is
   declared (every declared output is required on success). Known limit: a superseding upload that is
   interrupted midway leaves no complete checkpoint until the next one finishes.
6. **Diagnostics.** The reserved output `runtime-diagnostic.log` (at least 1 KiB) holds one JSON header line
   (`schema, status complete|partial, exit_code, truncated`) followed by the last 64 KiB of the entry's merged
   stdout and stderr, cut to the declared cap. It is uploaded on success, failure and timeout whenever the entry
   started. The status line gains the child's `exit_code` and, for a partial upload, `partial` and the outputs.
7. **Data policy for the tail.** The tail is untrusted entry output. It is stored in custody like any other
   output (quarantined, imported only after the stop is confirmed), never printed by the runtime, never copied
   into a status line, cause, receipt or log by the guardian, and never interpreted. The runtime adds nothing
   to it (no environment, path or token). The entry's environment holds only the reviewed contract env, which
   the contract already refuses to carry secrets; an entry that prints a secret it was given leaks it into its
   own quarantined diagnostic only, which is why the tail is bounded and why consumers read it as text with
   replacement of invalid bytes.

## Consequences

- The guardian status gains `workload_exit_code` (additive); the live receipt's stop cause may read
  `workload_failed:<reason>` and the receipt reason carries it through, so the campaign verdict is `failed:
  live receipt not passed: workload_failed:<reason>`.
- Contracts without the reserved names, and images built before this change, behave as before; an old image
  never calls the new endpoint and exits as it did.
- A later resume step reads the latest checkpoint and classifies retry versus terminal; this ADR only guarantees the
  bytes and the cause are there.
- A fixed 120 s wait after an early finish is billed time (bounded by the existing upload grace policy).
