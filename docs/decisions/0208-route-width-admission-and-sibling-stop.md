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
