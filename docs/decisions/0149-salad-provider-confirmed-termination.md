# 0149 — A reallocated Salad route ends on the provider's own confirmed stop

## Status

Implemented (no schema change). Narrows the Salad settlement of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and the executor of ADR 0038's amendment for one case:
a route whose allocation changed. Every route fence, guardian rule
([ADR 0041](0041-execution-frames.md)) and data rule
([ADR 0040](0040-multi-stage-multi-vendor-campaigns.md),
[ADR 0044](0044-salad-review-preapproval.md), [ADR 0047](0047-agent-routed-data-destinations.md))
is unchanged, and no new network destination is added.

## Context

When Salad moves a running instance to another machine, the route records `reallocated` and
never again sets `termination_confirmed` (retained output cannot be imported, the group cannot be
started or deleted through that route). The guardian stops the group and, by its own rule, never
confirms the stop itself after an allocation change; its provider-confirmed close
(`provider_confirmed_stop`) is its own read. Nothing on the control side ever recorded the
provider's confirmation either: the executor reported the confirmed exit only for a route with
`termination_confirmed` and no reallocation, so verification answered `unknown`, the stage went
`blocked_unknown`, and the attempt's hold (and the campaign's active input release) stayed until a
person reconciled the grant. Runs that hit repeated reallocations all ended this way.

## Decision

1. **Only a provider read recorded on the route confirms.** `salad_termination.provider_termination`
   is a pure read of the route state. For a reallocated route it looks at readbacks with
   provenance `provider` (accepted by the trusted intake only for a claimed effect of the bound
   server) of effects requested at or after the route's stop. The latest such readback must read
   the exact group `stopped` with termination confirmed (no instance, every count present and
   zero, no change in flight) under the route's provider identity. A later adverse readback undoes
   it. The guardian's status, a worker report, a synthetic readback, or a read from before the stop
   never confirm anything; the guardian keeps its rule of not self-confirming.
2. **The executor polls, bounded.** After the live driver returns for a reallocated route that has
   a stop but no confirmation, the executor sends one route `observe` (the trusted server's
   credentialed exact read, the same composition as the live driver's) every
   `termination_poll_seconds` (default 15, 5 to 60) until the reading confirms or
   `termination_confirm_seconds` pass (default 600, 0 to 3600; 0 disables the wait). A refused
   observe (fenced route, spent effect budget) ends the wait at once; three failed reads in a row
   end it too. It never sends create, start, stop or delete.
3. **Confirmed: the existing path.** The executor reports the exited observation once (its fixed
   idempotency key) at the confirming read's time. Control re-reads the route itself, scoped to the
   campaign's org and project, and requires both the confirmed reading and a confirmed exit before
   it records the verdict `failed` (outputs are never accepted after a reallocation). The attempt
   then settles exactly as any failed Salad run: a recorded charge, a human reconcile, or (when its
   guardian final is clean, which a reallocation is not) the bounded hold. When the campaign
   finishes, its input release is revoked by the existing completion step, so another campaign can
   take the same snapshot.
4. **Unconfirmed: held and visible.** With no confirmation at the bound the attempt becomes
   `unknown` with the code `termination_unconfirmed` (or `termination_unconfirmed_new_allocation`
   when a provider read showed a different allocation still present) as the Salad row's waiting
   reason, and one `campaign.salad_termination_unconfirmed` event (`hold_released: false`), in one
   runner transaction. The campaign blocks as before; nothing is settled or released and the input
   release stays active. The human reconcile path is the way out.
5. **Visible.** Stage status shows the code as the latest attempt's waiting reason. The campaign
   receipt's Salad attempt carries `salad_termination` (status, reason, confirming read time and
   digest, `new_allocation_unconfirmed`, provider reads after the stop, `hold_released`) for a
   reallocated route only, so every other receipt keeps its shape.

## Consequences

- A run whose allocation changed no longer blocks its campaign when the provider confirms the
  group stopped; it ends as a failed attempt waiting for its charge like any other failed run.
- Each poll is one more route effect, within the route's existing effect budget.
- The executor holds the attempt for up to `termination_confirm_seconds` after the driver returns.

## Not covered

- A confirmation that arrives after the bound: the attempt stays blocked until a person reconciles
  it (the executor does not revisit an attempt whose outcome is recorded).
- The guardian's own `provider_confirmed_stop` is not used as evidence or as a trigger; the
  executor polls regardless of it.
- Settling at the guardian-bounded hold after a reallocation: the guardian final is not clean, so
  that guard is unchanged and a charge or human reconcile settles it.
- Choosing a different GPU class after repeated reallocations (placement, a separate change).
