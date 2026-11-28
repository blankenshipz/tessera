# 0208 — Admission follows route width, free retries wait only for unresolved cost, a sibling's failure keeps 0 cent retries

## Status

Accepted. Amends ADR 0207 (lease-window requeue), ADR 0205 (exposure after a create) and ADR 0058
(infrastructure retries). No schema change and no migration. Nothing here loosens a claim,
admission, spend, cancel (reduce-only) or sealing guard.

## Context

A wide official `failed_stages` rerun of an out-of-distribution campaign had to be sliced by hand to
the executor's route width. Three rules combined:

1. The runner issued grants up to the project concurrency; the executor leased every grant and only
   then applied `max_concurrent_paid_routes` (1 to 4). Every attempt outside the first wave lapsed
   (`lease_window_short_before_route`, `not_started`) and burned its free requeues.
2. Any Salad attempt of the campaign in `leased` .. `executing` blocked every new positive hold, so a
   stage released at 0 cents could not retry while a healthy sibling ran.
3. A sibling's `stage_failed` stop reason ended every `failed_transient` stage as `<class>_exhausted`
   before `retry.infra_retries` was compared, while already-queued routes kept launching.

## Decision

**Admission width (M1).** The operator may record the executor's width for control as
`salad_max_concurrent_routes` (1 to 4) in the campaign-providers file. When recorded, the Salad
provider answers the runner's existing `slot_free` / `free_slots` hooks: a slot is occupied by every
Salad attempt, of any campaign, that holds a grant, whose status is not terminal, and that is in
`awaiting_lease` (or has no Salad row yet) through `executing`. Status is read by exclusion from the
terminal set (`succeeded`, `failed_transient`, `failed_permanent`, `unknown`, `refused_budget`,
`cancelled`), not from a list: `granted` after `issue_grant`, `admitted` after `admit_attempt` (held
through every pre-route phase), `running` from `route_started`, and any other non-terminal status
all count. The Salad provider keeps this as one definition (`ROUTE_SLOT_SQL`) that every count
reads. `outcome_recorded`, `awaiting_charge` and `unknown` no longer occupy a route. The stage waits
at `campaign.stage_waiting_slot` with no grant (the grant lifetime starts when it is issued), so no lease clock starts for work that cannot start. The slot wait is not
FIFO: when a slot frees, the first waiting stage the runner reaches takes it (the FIFO order of
`waiters_ahead` applies only to project limits). Placement counts the same slots for a
stage with candidates. Unrecorded means unbounded, as before; an operator whose executor is narrower
than the project concurrency must record the width. An invalid width refuses the providers file.

**Free retries (M2).** A retry whose latest attempt is `failed_transient`, ended for a class that
keeps its own bound (below) and settled at 0 cents asks the provider for
`unresolved_exposure_open` instead of `exposure_open`: only `outcome_recorded`, `awaiting_charge` and
`unknown` block it. A provider without that probe keeps the whole gate; the campaign cap, the program
budget check, the `unknown` refusal and the dropped-provider check are unchanged.

**Sibling stop (M3).** When the campaign's stop reason is `stage_failed:...`:
- a stage whose latest attempt is an `infra_*` class, `provider_reallocated_before_custody` or
  `prestart_no_spend` keeps each approved retry bound: `retry.infra_retries` for its class,
  `retry.max_class_fallbacks` for a pre-start move, `scientific >= max_attempts` and the attempt
  number cap; `not_started` ends as `not_started_campaign_stopped`, not `_exhausted`;
- a `route_started` attempt that is not itself a retry of such a failure, with nothing sent (the
  existing `_never_sent` proof), is listed to the executor as held (`stage_failed_stop`, never
  launched) and released at 0 through the existing unclaimed unwind as `not_started`.
Cancel (`cancel:`), budget and stop-rule stops behave as before.

**Report wording.** The campaign report's "not retried because the campaign was already stopping"
is used only when the stop could have ended the retry: a cancel, a budget or stop-rule stop, or a
sibling's failure for a class that does not keep its retry. A stage whose last attempt is a class
above that failed under a sibling's stop used up its own bound, and the report says so. A campaign
run before this change that ended that way is reworded the same, since its records cannot show the
order.

## Consequences

- A 28-stage campaign on a width-4 executor issues four grants at a time and spends no free requeue
  waiting. Waiting stages still reserve their hold and release before the slot check on the
  single-provider path (as for any worker provider); they hold no grant or lease.
- A rerun that includes a stage that may fail again no longer ends its siblings' 0 cent retries.
- A route queued behind the executor's own limit when a sibling fails is released, not launched.

## Not done

The executor does not report its width to control; the operator records it. Un-failing stages of a
finished campaign is not offered: the rerun is the remedy. A stage still waiting for a slot when a
sibling fails later takes a grant, a lease and a route slot before it is stopped at `route_started`
and released at 0 (nothing is sent or spent). A stage that never started is reported `failed`, not
`not_started`.

## Addendum: a blocked campaign gives back its unleased grants; `unknown` keeps its route slot

**Context.** The width counts every grant-holding, non-terminal Salad attempt of any campaign. An
unleased grant was given back only by a paused campaign (ADR 0167, worker-grant providers only)
or on expiry (ADR 0153, hours later). A live rerun went `blocked` with three grants holding no
route at width 2, so every Salad campaign waited for a slot until those grants expired. The
executor also leased an `awaiting_lease` item without looking at its campaign's status, and an
`unknown` attempt freed its slot although its Salad route might still be running.

**Decision.**
1. **Blocked releases like paused.** One condition (`unleased_release_reason`) decides whether a
   campaign gives back the grants no worker took: paused (`paused_delegation_lapsed`, or
   `running` with a steering pause) or `blocked`. The runner checks it on each pass and again
   inside the write transaction. The evidence and the accounting are ADR 0167's, unchanged. The
   attempt is `granted` or `admitted`, unsettled, and has no operation. The grant is `active`,
   with no lease row of any age and no external operation. The hold moves to settled at 0
   (`reconcile_grant_row`). The attempt ends `failed_transient`/`not_started`, which uses an
   attempt number but no scientific retry. Unblocking places a new attempt through the normal
   `issue_grant` admission. Eligible providers are registered worker-grant and Salad route
   providers. A Salad attempt must still be `awaiting_lease` with no lease or operation on its
   Salad row, and its row moves to `unclaimed` in the same transaction, so it is never listed
   to the executor again. Holdout stages and any other provider kind keep their grants. This also
   applies the ADR 0167 release to a paused campaign's unleased Salad grants (it covered worker
   grants only). For worker grants, paused behaviour is unchanged. A blocked release writes
   `execution.blocked_unleased_grant_released` and a `campaign.slot_freed` (reason
   `blocked_unleased_grant`, trigger `blocked_campaign_release`). A blocked campaign's input
   releases stay under the idle-campaign rule (ADR 0153), as before.
2. **A leased grant stays.** A grant with any lease or operation is in flight. It keeps its
   slot until the evidence paths (ADR 0053, ADR 0153) settle it or a person reconciles it.
3. **The executor takes no lease for a campaign that may not run it.** An `awaiting_lease`
   item is leased only while its campaign is `running` (not steering-paused) or `stopping`.
   Control lists the work with `steering_paused`. The executor skips any other item, never
   failing it, and logs `lease_held` with the status (`paused` for a steering pause). `stopping`
   stays leasable because ADR 0038 lets outstanding attempts finish: refusing the lease would
   strand the grant, and its route slot, until expiry. An item from an older control without
   `campaign_status` is leased, as before. A lease the executor already holds is not
   abandoned.
4. **`unknown` keeps its slot until the route end is confirmed.** `unknown` is still terminal
   for `ROUTE_SLOT_SQL`. In addition, an `unknown` Salad attempt that holds a grant and has a
   route counts as occupied until control's latest route record confirms the route stopped. A
   reallocated route needs the provider's own read of the stopped group (ADR 0149). Any other
   route needs termination confirmed, provider status `stopped` and a provider readback.
   Without a route record, the slot stays occupied. An `unknown` attempt without a route frees
   its slot (nothing was started on Salad). Leaving `unknown` through any reconcile frees it,
   as any terminal status does.

**Consequences.** At width 2, the live shape (blocked, three unleased grants) frees its slots on
the next runner pass. A waiting stage of another campaign then takes a slot through the unchanged
admission path. It gave its input release back while it waited, so it is granted a pass or two
later. An `unknown` attempt whose route end is unproven now holds a slot until it is
reconciled, which can narrow admission. This is the fail-closed choice: control never grants
more routes than the executor may be running. Nothing loosens a claim, admission, spend,
cancel or sealing guard. Every new path only gives back authority nobody used, or counts more.

