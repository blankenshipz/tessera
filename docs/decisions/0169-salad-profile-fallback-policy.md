# 0169 — A Salad stage changes profile (GPU class) only before any possible start

## Status

Accepted. Narrows [ADR 0151](0151-salad-alternate-execution-profiles.md) (alternate execution
profiles), [ADR 0148](0148-salad-allocation-instability-fallback.md) (allocation instability) and
the class fallback addendum of [ADR 0060](0060-capacity-aware-stage-placement.md). No schema change
and no migration. Point 3 is narrowed by one exception in
[ADR 0191](0191-salad-cpu-fallback-and-concurrent-frames.md) (section 1, item 9): a CPU stage whose group was started but
never given an instance, proven from control's own guardian records, may move after an `infra_allocation_timeout`.

## Context

Two pieces of work let a Salad stage run on another operator profile (one GPU class each) than the
one it started with: a move after an attempt that provably never started and spent nothing (ADR
0060 addendum), and a move after a run whose allocation proved unstable (ADR 0151 as first
drafted, triggered by ADR 0148 or ADR 0149 evidence). They defined the list twice, with two caps
and two ways of choosing the next profile, and the second moved a stage to another class after a
run that had started and could have been billed. A move after a possible start changes the
hardware an experiment ran on part way through its attempts, and spends again on a new class
without a person seeing why the first run's money bought nothing.

## Decision

1. **One list.** `SaladStageSpec.alternate_profiles` (ADR 0151: one to four distinct operator
   profiles after `profile`, absent from the serialized spec when unset) is the only list. Its
   length cap and `retry.max_class_fallbacks` are the same number (4). The next profile is always
   `salad_alternate_profiles.next_profile(approved stage, tried)`, where `tried` is the stage's
   own profile plus the `to_profile` of every recorded `campaign.salad_profile_switched` event on
   that candidate.
2. **The strict rule.** A stage moves to its next listed profile automatically only before any
   possible start: the provider proves from its own records that the attempt never started (no
   `start` effect on its route, never reallocated, a live receipt naming the pre-start step or a
   create rejection) and the attempt settled at exactly zero. Such a move is bounded by
   `retry.max_class_fallbacks`, spends no scientific attempt, counts in the approved worst case
   (`max_attempts + infra_retries + max_class_fallbacks`) and must pass every check an approved
   profile passes (same per-run cap, the capability declaration, a review pre-approval binding its
   plan digest); an uncovered next profile holds the stage (`profile_not_approved`) instead.
3. **After any possible start or spend there is no class move.** An attempt that ran unstable
   (ADR 0148 window, or ADR 0149 provider-confirmed stop of a reallocated route), or any attempt
   with a start effect or a non-zero or unknown settlement, keeps its own outcome and settlement
   path. For a stage that lists alternates and ended unstable, the stage is held (`blocked_unknown`,
   the campaign blocks with reason `post_start_switch_refused`) with exactly one
   `campaign.salad_profile_fallback_stopped` event (`started: true`, `spent` from the settled
   amount), and the stuck view's `attempt_outcome_unknown` finding for the stage carries
   `salad_profile_decision` (outcome, attempt, `started`) in its evidence: a person, or an
   approved delegation, decides. A retry of
   a stage keeps the profile its last attempt ran on (the runner's current profile changes only
   through a switch event). ADR 0148's placement reorder applies only while no earlier attempt of
   the stage may have started (none, or only attempts recorded `not_started` or
   `prestart_no_spend`).
4. **Every move is recorded.** Each move is one `campaign.salad_profile_switched` event, committed
   in the same transaction as the attempt's `prestart_no_spend` transition, with `provider_id`,
   `from_profile`, `to_profile`, `reason` (`prestart_no_spend`, the only reason),
   `prestart_reason`, `attempt_id`, `from_attempt`, `to_attempt`, `started: false`,
   `spent: false` and `settled_cents: 0`. The receipt's `salad_profiles.switches`, each attempt's
   `salad_profile`, the stage-status read and the runner's current stage are all derived from
   these events.
5. **Paired comparisons never cross classes.** The paired delta behind a gate (ADR 0045) reads
   the profile each paired attempt ran on from the same events; a pair whose arms ran on
   different classes, or a comparison whose pairs span classes, is refused with the closed code
   `class_mismatch`. A result on one Salad class names it as `execution_class`; results off Salad
   are unchanged.
6. **A heavy stage is never silently local.** When ADR 0148 moves an unstable remote class behind
   a local candidate for a heavy stage (ADR 0107), the guard records the explicit reason
   `allocation_unstable` with the remote candidate, so the placement carries the warning (or the
   operator's `refuse` mode skips it); without that record the local placement stays unexplained
   and refused by default.

## Consequences

- No approved stage spends twice across classes for one experiment step without a person's
  decision; the price is a held campaign when a class proves unstable after starting.
- Specs that listed alternates under ADR 0151's draft rule need `retry.max_class_fallbacks` of at
  least 1 (submit refuses alternates that could never be reached); `retry.max_attempts` no longer
  has to cover the list.
- Existing specs, their digests and receipts without alternates are unchanged.
- Out of scope: moves between *providers* the spec lists, which keep their own contracts: ADR
  0058's infrastructure reroute to a `retry.reroute_providers` target and ADR 0060's placement
  among `alternate_providers` when the candidate a retry would stay on is refused by a check.
  Neither changes a Salad stage's profile.
- Verified with fakes only; nothing here has run against live Salad.
