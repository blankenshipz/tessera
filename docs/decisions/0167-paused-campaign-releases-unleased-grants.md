# ADR 0167: a paused campaign gives back a grant no worker took

Status: Implemented (no schema change). Extends ADR 0153 (stuck campaigns release capacity
they provably are not using) to a campaign that is paused, and adds a fair queue for
project grant slots.

## Context

The project concurrency limit counts unresolved execution grants (`active` or
`cancel_requested`). ADR 0153 frees the grants of a blocked or `paused_delegation_lapsed`
campaign only once they have expired. A campaign paused through the steering route (ADR 0061)
stays `running`, and a pause stops only new stage reservations. A grant already issued for
an attempt no worker took (for example after a worker died before leasing it) stayed
`active` until its lifetime ended, so a project at its limit left a new campaign waiting at
`reserved`. Nothing ordered the waiters when a slot freed: whichever campaign the runner
ticked first took it.

## Decision

1. **Release on pause.** While a campaign is paused (`paused_delegation_lapsed`, or `running`
   with a steering pause) the runner reconciles each grant that no worker ever took, live or
   expired. The evidence, re-read inside the write transaction under the runner lease: the
   attempt is `granted` or `admitted`, unsettled and has no recorded operation; the grant is
   `active` (not `cancel_requested`); no lease row of any age and no external operation
   exists for it. The accounting is the shared `reconcile_grant_row` (hold moved to settled
   at zero). The store refuses a lease on a reconciled grant, so a worker cannot take it
   afterwards.
2. **Fail closed.** A grant with any lease or operation, a `cancel_requested` grant, a grant
   of a holdout stage, and a grant of a provider that is not a registered worker-grant
   provider are never released by this rule; they follow ADR 0153.
3. **No retry is consumed.** The attempt ends `failed_transient` with failure class
   `not_started` (the ADR 0046 class), settled at zero. It uses an attempt number but no
   scientific retry. The attempt numbers of a stage are still bounded, so a stage that is
   paused and released repeatedly can run out of numbers and fail as `..._exhausted`.
4. **Resume re-acquires through normal admission.** Resuming places a new attempt, which asks
   for its grant through the unchanged `issue_grant` path: the project and provider checks
   run, so a resume can never exceed the limit.
5. **Fair FIFO.** An attempt refused for the project limits is a waiter
   (`campaign.stage_waiting_limits` now carries `attempt` and `attempt_id`, once per attempt).
   A new grant request is admitted only when the unresolved grants plus the waiters ahead of
   it (earlier `created_at`, then id; same project; reserved, no grant, no `issue_grant`
   intent; campaign `running` or `stopping` and not paused) are below the limit. A resumed
   campaign's new attempt joins the back of the line; a campaign that arrives later waits
   behind it. A waiter that is paused, cancelled or finished stops counting.
6. **Recorded.** `execution.paused_unleased_grant_released` (grant, zero counts, zero
   settlement, `consumed_scientific_retry: false`) and a `campaign.slot_freed` event with
   reason `paused_unleased_grant` and trigger `paused_campaign_release` are written in the
   release transaction, once. The grant issued after a release writes
   `campaign.slot_reacquired` (`stage_key`, `attempt`, `attempt_id`, `previous_attempt_id`,
   `grant_id`) in the grant's transaction.

## Amendment to ADR 0061

ADR 0061 says a pause leaves "the running attempt" alone. This ADR draws the line at the
worker's lease. An attempt whose grant a worker has leased (or that has an operation) is in
flight and is left alone exactly as before. A granted but not yet leased attempt is **no
longer in flight for pause purposes**: nothing runs yet, so the pause gives its grant back
(rule 1) and the stage's next attempt is placed on resume. A pause between grant issue and the
worker's lease therefore voids that attempt (`not_started`, no retry consumed) rather than
letting it start. Everything else in ADR 0061 (new reservations stop, settlement and cancel
are unaffected, who may lift a pause) is unchanged.

## Input release of a paused campaign (amendment)

The active input release is a worker's authority to read a snapshot, and the unique active
index is a mechanical one-row rule, not a promise of exclusive use to a campaign. The
platform already revokes the release of a terminal, blocked or `paused_delegation_lapsed`
campaign once nothing needs it (`docs/campaigns.md`, "Input releases of idle campaigns"), and
a steering pause is the same state for this purpose. So no exclusivity is weakened.

7. **Release on pause.** On the same tick that gives back unleased grants, a steering-paused
   campaign's releases are revoked under the idle-campaign rule (`release_still_needed`):
   nothing is freed while any attempt using the release is in flight, `unknown` or
   unsettled, or a grant against it is unresolved. `campaign.release_freed` records it with
   reason `paused` and trigger `paused_campaign_release`. A revoked release is not active, so
   repeating the tick or the pause writes nothing.
8. **Re-acquire on resume, in line.** A campaign that gave a release back on pause waits when
   another campaign holds the snapshot instead of failing the stage as `step_refused`; a
   campaign that never gave one back keeps the original refusal. The wait is one
   `campaign.stage_waiting_release` event per attempt (`stage_key`, `attempt`, `attempt_id`,
   `snapshot_id`, `reason` `held_by_campaign` or `earlier_waiter`, `returning_from_pause`).
   Waiters are served first come, first served by attempt creation time: no campaign takes a
   free snapshot ahead of an earlier waiter, so a resume cannot race a new admission.

## Limitations

- The FIFO line counts any earlier waiter, including one that is stuck on a provider slot
  rather than the project limit, so such a waiter can hold back later attempts for other
  providers until it is admitted, paused or finished. The queue is not redesigned here.

- The release runs on the runner's tick, not inside the pause request.
- A leased grant whose worker died keeps its slot until the evidence paths of ADR 0053 and
  ADR 0153 settle it; this rule never guesses.
- The queue orders only attempts that were refused for the project limits; an attempt that
  waits for a provider slot or a hold is not in the line.
