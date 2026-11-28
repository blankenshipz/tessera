# 0227 — A non-zero exit after a failed provider read or a safety stop is retried as infrastructure

## Status

Accepted. Amends [ADR 0224](0224-reallocated-runtime-exit-retryable.md) and ADR 0207 (safety
stops). No control schema change and no migration. The guardian (safety service) gains two
reservation columns, `workload_read_failures` and `last_refusal_read_failure`, added in place by
its own startup column check.

## Context

Every bound-workload call (input read, claim, output, failure report) makes its own fresh Salad
provider read. When that read fails or times out with no allocation change, the call is refused,
and the GPU runtime exits 1 at `input_fetch` or `custody`. ADR 0224 retries such exits only on a
provider-moved frame, so this stayed `failed_permanent` / `salad_container_exited_nonzero`.

Separately, the guardian can stop a begun run for its own health: `provider_unavailable` after its
60 s post-start read window, or any other safety-plane cause. The attempt is then
`infra_salad_safety_stop` (retryable) when the container is killed (137 or 143) or nothing
exited. But the runtime (PID 1, which ignores SIGTERM during the run) usually exits 1 on its next
refused custody call first, so the start events read `[1, 143]`, and that was read as the
stage's permanent failure.

## Decision

1. **Record.** The guardian counts, per frame, staged-input reads refused because its own fresh
   provider read failed or timed out (`is_transient_read_failure`: a timeout or the transient
   `provider_unknown`, not a deterministic read failure). Only the input read counts: its token is
   verified for the bound machine before the provider read, while a claim, output or
   failure-report token is only checked after it. For every refused call whose binding matched
   the frame it also records whether that refusal was such an input read failure, so the flag
   describes the run's last refusal: a later refusal for any other reason (an output budget, a
   consumed claim, a passed deadline) clears it. The refusals themselves are unchanged. Both are
   exposed in its status (`workload_read_failures`, `last_refusal_read_failure`) and projected onto
   the recorded final only when set (a count of at least 1; the flag only when true). Stored finals
   keep their shape (`_stored_final` leaves both out when absent), and an older guardian projects
   as before.
2. **Classification.** A recorded non-zero exit on a frame not handled by ADR 0224, with
   `workload_read_failures >= 1` and `last_refusal_read_failure`, whose last failed runtime status
   line names the input read (`input_fetch` or `input_unavailable`, with no step or step `manifest`
   or `chunk`), and with no status line naming `entry` or `entry_timeout`, is `failed_transient` /
   `infra_salad_provider_read_failed`. A `custody` exit (claim, output plan, upload, failure report,
   or a runtime-local deadline or incomplete upload) is never explained by a read failure and stays
   permanent. It counts against `retry.infra_retries`;
   with none left the stage fails under the same class. Each retry is a fresh attempt with its own
   hold. Settlement is unchanged: the whole bounded hold on a clean final, or a recorded charge.
3. **Safety stop first.** A recorded non-zero exit whose recorded final carries a safety-plane
   `safety_stop_cause` (the closed table) and whose last failed runtime status line names
   `input_fetch`, `input_unavailable` or `custody` (once the frame is stopping every call is
   refused), with no entry line, is `infra_salad_safety_stop`, with the same retry bound and
   settlement (the whole hold) as any safety stop.
4. **Fail closed.**
   - Entry lines anywhere keep the exit permanent.
   - Other runtime reasons (output_plan, output, manifest, fetch, install, argv, internal) stay
     permanent.
   - No status line (unavailable, unread, sealed or cut) is an unknown cause and stays permanent.
   - Without the guardian's record the exit stays as today.
   - Exit codes are never consulted. `infra_exit_codes` is unchanged.
5. **Untrusted log.** The log tail is untrusted, because the stage writes to the same stream. It
   only narrows the guardian's records. The advisory `last_workload_refusal` is not used.

## Option evaluated, not taken

Skip or soften the per-call provider read for read-only input fetches when a guardian read
confirmed the bound running instance within the last poll window, keeping the JWT machine binding.
Not taken in this change:

- The per-call read is the check that the bound instance still runs on the bound allocation at
  the moment of the call, and changing the guardian's admission of workload calls deserves its own
  reviewed change.
- During a sustained read outage the loop's own reads fail too, so a recent confirmation would
  rarely be available exactly when it matters, while intermittent failures are now retried by
  decision 2.

Revisit if retries spent on intermittent read failures turn out to be frequent.

## Consequences

- Control and the guardian must both run this release for decision 2. An older guardian sends no
  count, so such exits stay permanent. Decision 3 needs no guardian change.
- Every infra retry recomputes the stage from zero. Raise `infra_retries` for long stages while
  provider reads are degraded.
- Residual risk: a stage that forges an input or custody status line and suppresses the runtime's
  own line could get at most `infra_retries` extra attempts, each within its own hold, and only on
  a frame where the guardian's last refusal really was an input read on a failed provider read, or
  where it really stopped for its own health. A caller with the frame's binding but no valid token
  can only clear the last-refusal flag (a refusal for another reason), never set it.
