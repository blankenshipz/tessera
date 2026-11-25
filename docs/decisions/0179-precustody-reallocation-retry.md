# 0179 — A reallocation before any custody work is retried on a fresh attempt

## Status

Proposed. No schema change. Refines the pre-start rule in
[Salad safety service, "Allocation change before anything ran"](../salad-safety-service.md)
and [ADR 0149](0149-salad-provider-confirmed-termination.md), and reuses the retry shape of
[ADR 0058](0058-infrastructure-retry-of-stages.md). Every guardian, custody, budget and termination rule
is unchanged.

## Context

A GPU smoke ran about six minutes and then the provider read showed a different allocation. No
output had been written and no input had been served, but the guardian stopped (the existing
rule: once an instance has run, a change of allocation is never re-bound), the group was stopped
and the receipt was `provider_stopped_custody_unsealed`. The attempt then failed permanently, so
a provider-side interruption that produced and received nothing ended the stage.

## Decision

1. **Classify.** The safety service status carries `custody_work_begun`: true once the workload
   called begin-once, an input was served, or an output was requested or written. A running
   instance alone is not custody work, and neither is the recovery a reallocation itself sets.
   When the guardian stopped on `allocation_changed`, the provider confirmed the exact group
   stopped, and the guardian says `custody_work_begun` is false, the live receipt's reason is
   `provider_reallocated_before_custody`. Any other combination, including an unknown answer,
   keeps `provider_stopped_custody_unsealed` and today's fail-closed behaviour.
2. **Report as retryable.** Control's reallocated-route verification (ADR 0149, unchanged: the
   provider's own confirmed stop and a confirmed exit are still required) carries the class on the
   attempt's failure detail only when the receipt names it, the stop cause is `allocation_changed`
   and `custody_work_begun` is false. The Salad provider reports that attempt as
   `failed_transient` with failure class `provider_reallocated_before_custody`.
3. **Retry on a fresh attempt.** The runner places a new attempt with its own grant, lease,
   hold and receipt. The interrupted instance is never re-bound. The retries are bounded by the
   stage's `retry.max_reallocation_retries` (0 to 3, default 2) and by the existing attempt
   number limit, and they do not use the stage's scientific `max_attempts`. Each attempt keeps its
   own lineage by attempt number; the campaign receipt lists every attempt with its hold and
   settlement. A holdout stage never retries (ADR 0068).
4. **The spend cap still governs.** A new attempt reserves its hold against the campaign budget
   cap like any attempt, and no hold is placed while an earlier attempt's exposure is open. The
   field is not serialized at its default, so existing specs keep their digest. The bound is
   shown to approvers anyway: each stage's `infra_retry_policy` entry carries
   `max_reallocation_retries` even at its default, and its `worst_case_attempts` includes it
   (as ADR 0058 did for `infra_retries`). The budget cap still limits the spend.
   The stage task budget is `max_exposure_cents` x (`max_attempts` + `infra_retries` +
   `max_reallocation_retries`), still bounded by the campaign budget cap and the open-exposure
   rule. Setting `max_reallocation_retries: 0` ends the stage as
   `provider_reallocated_before_custody_exhausted` (not `salad_not_passed`).
5. **After custody began, nothing changes.** A reallocation after custody work, with an unknown
   custody answer, or with any other stop cause, ends the attempt exactly as before.

## Consequences

- The interrupted attempt is billed for the time it ran. Its settlement is unchanged (ADR 0038 and
  0149: a recorded charge, a human reconcile, or the bounded hold when the guardian final is
  clean, which a reallocation is not), and the runner places the retry only after that exposure is
  closed. Until then the retry waits; it does not start without the hold being settled. Settling
  such an attempt without a person is a separate decision and is not made here.
- A retry can only help when the provider moved the instance; a stage that fails the same way each
  time ends with `provider_reallocated_before_custody_exhausted` and every attempt on the record.
