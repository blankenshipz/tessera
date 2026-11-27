# 0207 — Salad infrastructure stops retry without a person, and waiting never spends a route

## Status

Accepted. Amends ADR 0206 (items 4 and 5: estimate settlement and the provider-confirmed
reallocation), ADR 0046 (pre-route requeue), ADR 0038 (bounded-hold settlement) and ADR 0041
section 7.2 (the open-frame fence). No schema change and no migration: every new record is an
additive, closed field of the executor's recorded guardian final or the live receipt.

## Context

A static review of the Salad runtime after ADR 0206 found five ways to lose a stage, or stall a
campaign on a person, with a healthy workload:

1. Settlement runs before the retry decision. `infra_salad_reallocated` is transient, but its
   guardian final is `reconciled` (the safety service's provider-confirmed close of an uncertain
   allocation), which no automatic settlement accepted. The attempt waited in `awaiting_charge`
   for a person, and its retry with it. A live campaign then stalled the same way after a run that
   had retained its outputs: Salad changed the allocation while the group was stopping, so the
   guardian could only close the frame by the provider-confirmed reconcile and never sealed
   custody (`provider_stopped_custody_unsealed`, no manifest).
2. Control recognised only three guardian evidence shapes as infrastructure. Every other stop the
   safety plane makes for its own health (control silence, a service restart, an unreadable
   provider or rate, an uncertain clock, a failed loop pass) ended `salad_not_passed`, permanent.
3. The lease's hard expiry is fixed at issue, before packaging. Review and queue time came out of
   the route window; a cut at that shortened deadline is the permanent `route_deadline_reached`.
4. The open-frame fence was enforced only at the reserve, after the executor had marked the
   attempt executing. A fence refusal (an orphan frame still closing) spent an infrastructure or
   scientific retry.
5. Lease renewal had gaps: a lease taken earlier in the same pass, every lease during a route's
   finish (termination confirmation of up to 600 s, import, upload), and the running route's own
   lease during its child's tail.

## Decision

1. **A provider-confirmed move settles at the guardian's estimate.** For
   `infra_salad_reallocated` and `provider_reallocated_before_custody`, a recorded guardian final
   that is `stopped`, or `reconciled` with the reason `provider_confirmed_exact_group_stopped`
   (the safety service's own fresh exact read of the group stopped with zero instances), with
   exactly the `allocation_uncertain` flag that close implies, settles at the guardian's accrued
   estimate rounded up, at least one cent (custody may have begun, so never zero), within the
   hold. Control's own read of the guardian frame record must say `stopped` or `reconciled`. The
   basis stays the provisional `guardian_bounded_hold`; a recorded charge raises or confirms it.
   An operator's attested reconcile, any other flag, a missing, non-finite or above-hold estimate,
   or a billed cost still waits for a person. ADR 0206 item 5's "settles like any failed attempt"
   is replaced by this rule.
2. **A run that retained its output and was then moved is the same infrastructure move.** The
   guardian's status says `output_retained` when retaining the output was the operation's first
   stop decision. The executor records `retained_before_move` on the guardian final only when that
   holds and the final is the provider-confirmed reconcile of an uncertain allocation. Control then
   records `infra_salad_reallocated` (outputs never accepted), settles under item 1 and retries
   within `retry.infra_retries`. Custody is not sealed: the guardian still cannot prove a writer
   died after an allocation change, so sealing those retained outputs is left open (see
   Consequences). ~~Without that record the attempt waits for a person as before.~~ Superseded by
   the 2026-11-27 amendment below: a provider-confirmed reconcile settles at the estimate without it.
3. **Safety-plane stops are infrastructure.** `campaign_infra_retry.SAFETY_STOP_CAUSES` is a
   closed table: `heartbeat_silence`, `control_disconnect`, `missed_poll`, `clock_uncertain`,
   `guardian_restart`, `loop_frame_failed`, `rate_unknown`, `provider_unknown`,
   `provider_unavailable`. The executor carries the guardian's first stop cause on the recorded
   final (`safety_stop_cause`) only when it is in that table. Only that recorded value selects the
   transient `infra_salad_safety_stop`, never a receipt reason. It spends `retry.infra_retries`
   through the same path as the other placement classes, then the class/profile fallback; with
   none left it fails permanently under its own class. `cost_cap`, `total_cap`, `deadline`,
   `control_cancel`, `workload_failed:*`, `start_failure_suspected`, `allocation_changed` and
   `allocation_timeout` are never in the table. Settlement is unchanged (the whole bounded hold on a
   clean final; a provider-confirmed close of the frame is accepted for this class too). A
   `rate_unknown` final still waits for a person: no estimate bounds it. The prestart
   `infra_salad_guardian_unavailable` class stays as its own decision and shares the retry path.
4. **Waiting before the route never spends the route.** Before `start_route`, and while a started
   route has no create or start effect, no provider reference and no live receipt, a lease that
   has waited more than 600 s since issue and whose remaining hard life is shorter than the route
   window is unwound at 0 on the store's own no-claim proof and requeued `not_started`
   (`lease_window_short_before_route`) for a fresh grant and lease, within ADR 0046's repeat
   bound. An approved review carries over to the requeued attempt like any pre-route lapse. The
   executor applies the same rule 120 s earlier before `mark_executing`, and defers instead of
   launching. The 600 s tolerance leaves at least 900 s of the 1500 s provisioning allowance; a
   lease a grant or credential clamped from issue is never short before it has waited, since a new
   lease would be no longer. Issuing the hard expiry when the route starts remains a possible later
   lease-semantics change; it is not needed for this rule.
5. **The open-frame fence is asked before an attempt is committed.** The safety service answers a
   read-only admin `open_frames` request with the frames that may still bill, counted exactly as
   the reserve counts them (never-bound frames included), and its `max_open_billable_frames`. In
   both executor modes, before `mark_executing`, a full or unreadable answer defers the launch
   (`route_deferred`) and the attempt stays `route_started`, kept alive. For the remaining race,
   the live receipt records the guardian's last transient reserve refusal
   (`create_preflight_refusal`), and a never-sent create refused `single_operation_orphan_exposure`
   is requeued `not_started` (`guardian_frame_fence_before_create`) within ADR 0046's repeat
   bound, then handled as any never-sent create. The frame limit, the 900 s margin of a create
   that may have been sent, and the counting of never-bound frames are unchanged.
6. **Every lease an executor holds stays renewed.** A lease taken earlier in the same pass is kept
   alive by the serial child's keepalive. A route's finish runs under the same keepalive in serial,
   resumed and concurrent modes. The running or finishing route's own lease is renewed by the
   executor as well (its child sends none in its tail); a refusal of it is noted and stops that
   renewal but does not fence the route in the executor. Every renewal stays bounded by the
   lease's hard expiry, its epoch and an active grant.

## Consequences

A reallocation, a safety-plane stop or a moved finished run costs one infrastructure retry and a
provisional estimate-or-hold charge, not a person's charge or the campaign. A long review or queue
costs a fresh lease, not the run. A full open-frame fence costs a wait. No stop, cap, deadline or
authority check is loosened, and nothing infers termination, a charge or a refund.

Open: sealing outputs that custody retained before an allocation change during stopping. Today
those outputs are lost and the stage reruns. A safe seal needs proof that no writer on the moved
allocation could have written after retention; until then the run is retried, never accepted.

## Amendment (2026-11-27): attempts recorded before the move markers

A guardian final closed by the safety service's own provider-confirmed reconcile
(`RECONCILE_PROVIDER`) now settles the same way whether or not the attempt carries
`retained_before_move` or a move class, so an attempt recorded by an older release leaves
`awaiting_charge` without a person recording a charge. The rule is unchanged: control's own read
of the guardian record must confirm the close, the estimate must be finite and within the hold
(rounded up, at least one cent), no other flag may be set, and an operator's attested reconcile
never qualifies. A safety-plane stop keeps settling the whole hold. The settlement stays
provisional (`guardian_bounded_hold`); a human charge already recorded wins and is never doubled,
and a later charge adds only its excess.

## Amendment: a lease no fresh lease could beat is not requeued in a loop

The short-window requeue (decision 4) helps only when a fresh lease would be long enough. Two
shapes made every fresh lease as short as the one it replaced, so the attempt was requeued until
the identical-repeat bound and then spent a scientific retry:

- the route window was longer than any lease is issued for (the window may reach the plan bound,
  declared duration plus 3600 s, while a lease covers declared duration plus the 1500 s
  provisioning allowance);
- an expiry bound capped every fresh grant and lease below the run: the worker's or approver's
  credential, the campaign approval, or the stage's grant lifetime.

1. **What a lease must still cover** is the route window, but never more than a lease is issued
   for: `min(route window, declared duration + provisioning allowance)`. Control and the executor
   use the same measure (`campaign_salad_lease_cap.needed_seconds`), so the executor never defers
   a launch control would not requeue.
2. **Before requeueing**, control reads the latest hard expiry a fresh grant and lease could have:
   the earliest of the campaign approval's expiry, the approver's and the worker's unrevoked
   credentials, and now plus the stage's grant lifetime. If that leaves less than the run needs,
   the attempt is unwound the same way (nothing sent, released at 0 on the store's own no-claim
   proof) and closed `failed_transient` / `lease_capped_by_expiry`. That class is neither a
   scientific retry nor a not-started requeue. The campaign event
   `campaign.salad_lease_capped_by_expiry` records the binding bound, the seconds needed and the
   seconds available, and the attempt's failure detail says the same.
3. **For a renewable bound the stage waits** instead of placing another attempt while a fresh
   lease still could not cover the run (one `campaign.salad_lease_cap_waiting` event). It
   continues once that bound is lifted: an operator renews the worker credential through its own
   renewal path, the approver's credential is renewed, or the campaign is re-approved (for the
   approval bound); an expired approval pauses the campaign as before. An approved review carries
   over to the next attempt like any pre-route lapse.
4. **The stage's grant lifetime is not renewable**: `grant_ttl_seconds` is part of the approved
   spec, so no renewal or re-approval lifts it and waiting could never end. Salad admission
   refuses a stage whose grant lifetime is shorter than what a lease must cover
   (`min(route window, declared duration + provisioning allowance)`, the route window at most the
   profile's `route_max_seconds`) with `lease_bound_by_grant_ttl`. A stage admitted before that
   rule whose lease is capped by its grant lifetime is unwound the same way (released at 0) and
   closed `failed_permanent` / `lease_bound_by_grant_ttl`: the stage fails with that
   configuration reason, telling the owner to raise `grant_ttl_seconds` or lower
   `max_duration_seconds`. It is not a scientific retry. The grant lifetime is checked first, so a
   renewable bound that happens to be earlier never makes such a stage wait.

Nothing here renews a credential, extends a grant or an approval, or loosens a stop, cap or
deadline: the change only declines a requeue that could not help.
