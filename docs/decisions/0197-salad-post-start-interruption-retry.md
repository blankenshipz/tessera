# 0197 — A post-start Salad interruption is a bounded retryable outcome that resumes from a salvaged checkpoint

## Status

Proposed. No schema change. Extends [ADR 0179](0179-precustody-reallocation-retry.md) (the
pre-custody case) and reuses the retry shape of
[ADR 0058](0058-infrastructure-retry-of-stages.md). Every guardian, custody, claim, budget and
termination rule is unchanged, in particular the single-use claim of the runtime and the
provider-confirmed termination of [ADR 0149](0149-salad-provider-confirmed-termination.md).

## Context

Salad can move or interrupt an instance without warning. When that happens after custody work
began, the guardian stops with `allocation_changed`, the route's own termination fence confirms
the stop, and the attempt could only end `failed_permanent / salad_not_passed`. Neither
`retry.infra_retries` nor `retry.max_reallocation_retries` covered it, so a stage with
`max_attempts: 1` was dead after one provider-side interruption, even when a checkpoint had
already reached custody. Separately, a CPU-only group is created with priority `high`, but Salad
runs a group without a GPU at its lowest priority whatever was sent, so a readback that insists on
`high` can become a deterministic `identity_mismatch`.

## Decision

1. **Classify.** A reallocated or guardian-stopped attempt whose live receipt says custody work
   HAD begun (`custody_work_begun` is literally true), whose stop cause is `allocation_changed`,
   and whose stop the provider's own read confirmed (the route fence and a confirmed exit are
   still required) fails with the new class `salad_interrupted`. An unknown or false custody
   answer keeps today's behaviour (the pre-custody class for false, `salad_not_passed` for
   unknown); an unconfirmed stop stays `unknown` and held, as today.
2. **Retry.** `salad_interrupted` is `failed_transient` and counts as an infrastructure attempt:
   it spends one `retry.infra_retries` and is rerouted like any infrastructure failure. The bound
   `max_attempts + infra_retries + max_class_fallbacks` therefore stays an upper bound. With no
   retry left the attempt and the stage fail permanently under the same class (the stage reason
   is `salad_interrupted`, never `salad_not_passed`). The retry is a NEW attempt and operation
   with its own claim and hold; a claim is never reused or released for it.
3. **Settle at the estimate.** The interrupted attempt settles under the existing provisional
   `guardian_bounded_hold` basis, but at the guardian's accrued estimate (rounded up, at least
   one cent), not the hold. Control's own `stopped` read of the guardian record is still required,
   and the final must be clean except for the `allocation_uncertain` flag an allocation change
   itself sets (accepted only when the final's own reason is `allocation_changed`). An estimate
   above the hold bounds nothing: the attempt waits for a charge as before. A later recorded
   charge raises or confirms the amount through the existing late-charge path.
4. **Salvaged checkpoint.** When the interrupted attempt has a complete checkpoint in custody,
   the runner records `campaign.interrupted_checkpointed` once with its sha256 and size. The
   checkpoint is the stage output whose path is the workload contract's reserved
   `checkpoint.bin` ([ADR 0198](0198-workload-failure-custody.md): custody keeps only the newest
   complete upload of it, so a manifest lists at most one entry); it is read from the attempt's
   own custody manifest (bound to its grant, lease, operation and epoch, in the campaign's
   project) and must fit the declared output's byte cap. That manifest is a **partial
   manifest**: an interrupted attempt never uploads its diagnostic, so the executor imports the
   guardian's sealed, incomplete custody and writes a manifest listing only the declared files
   that reached custody, each hash-verified on import and again at admission (a partial
   manifest may be written only for this end state, and only with files the plan declares;
   control itself admits one only when all of its own records agree: the attempt's route (read
   within the owning campaign's org and project) is flagged reallocated, the provider's own read
   confirmed the stop, and the guardian's custody status, which the trusted executor reads over
   the authenticated admin channel and records before the upload, says custody work had begun
   on an allocation change. It refuses a partial manifest for any other lease, and when any one
   of the three is missing, so a precustody reallocation, a premature partial or an
   ordinary-run partial is never admitted and never blocks the complete manifest. Linking the
   manifest to the attempt repeats the check against the recorded live receipt).
   Custody is never read by operation instead. A manifest that lacks a declared output is
   never a pass: the executor's success check, the Salad verdict and an ordinary run's
   observation all refuse it, whatever receipt comes with it. The interrupted attempt's verdict stays
   `failed`, never `ok`; none of its other outputs is accepted. A stage with no declared
   checkpoint, or an attempt with none in custody, records nothing and the retry starts clean.
   A checkpoint is resumed only by the attempt immediately after the one that produced it: once
   a later attempt ended for any other reason, an older checkpoint is never bound again.
   **Hand-off:** the retry's input snapshot is composed with the checkpoint as one more manifest
   source, under the declared output's name and by the recorded digest, before the retry's trust
   check, release and grant; the retry's recorded inputs and the campaign receipt list it by
   digest. The interrupted attempt's manifest is released to custody under the same spec
   pre-approval a collected attempt gets (composition reads released custody only); that is
   custody, not acceptance. The snapshot is private-derived data, so the provider must be trusted
   to receive that class or the retry is refused like any other input. A retry with no
   checkpoint gets the base composition again, so a later attempt never inherits an older
   checkpoint's snapshot. Reading the file in the workload is the runtime's resume contract.
   Open with the runtime lane: whether a failed or interrupted attempt carries its custody
   manifest (`manifest_id`) after the superseding uploads is not defined by ADR 0198; if it does
   not, nothing is salvaged and retries start clean (fail closed).
   A route-deadline attempt records no checkpoint yet; that behaviour is deferred to
   that decision.
5. **CPU priority.** The create body keeps sending `high` (changing it would change reviewed
   request digests for no behavioural gain). For a CPU-only selection (`gpu_count == 0`) the
   readback accepts `high`, `batch` or no value; any other level is drift. A GPU group still
   requires `high`. The CPU ADR records that CPU runs are lowest priority and preemptible.

## Consequences

Provider interruptions after the start no longer kill a stage that opted into infrastructure
retries, and the spend of the interrupted attempt is settled at what it accrued, not at the hold.
Each retry holds its own exposure under the campaign cap. A recorded checkpoint is identified only by digest.

## Tests

`tests/test_salad_interruption_retry.py` (classification, one retry spent, settlement at the
estimate, unconfirmed stays held, exhaustion, checkpoint recording and binding, clean retry) and
`tests/test_salad_cpu_priority_readback.py` (CPU readback priorities, GPU unchanged).

## Amendment: a non-zero container exit is not an interruption

A group whose recorded system events name `instance_exited` and whose earliest non-zero exit code
is not 137 or 143 is no longer a `salad_interrupted` (or pre-custody reallocation) retry, whatever
stop cause the
guardian recorded. Control checks it before those classes and records `failed_permanent` with the
failure class `salad_container_exited_nonzero`, so one non-zero exit on one machine fails the
attempt without spending `retry.infra_retries`. A node-local fault that exits non-zero therefore
gets no retry. An interruption with no such exit keeps this ADR unchanged. When the earliest
non-zero exit is 137 or 143 (a kill or platform stop came first), the interruption path is kept.
