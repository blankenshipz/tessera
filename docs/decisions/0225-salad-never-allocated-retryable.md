# 0225 — A Salad route that never got a machine is retryable infrastructure, settled at its estimate

## Status

Accepted. Extends the never-placed rules of [ADR 0206](0206-salad-provisioning-not-run-time.md) and
the guardian's allocation timeout. No schema change and no migration. **Upgrade the Salad safety
service before control**: control reads a new optional key of the guardian's final; an older
guardian never sends it and every route classifies as before.

## Context

A stage's Salad route was never allocated a machine within about an hour and ended at its deadline
or by the provider's own stop. The guardian's own `allocation_timeout` record covers a group it
cancelled (or whose route limit it reached) with no machine ever placed. A group the provider
ended itself, with no stop decision of the guardian's own, left no such record: the attempt fell
through to `failed_permanent` / `salad_not_passed`, the slice stopped, and the whole hold settled
against a small estimate. Nothing ran, so this is provider capacity, not the stage.

## Decision

1. **The guardian says it.** A closed frame (`stopped` or `reconciled`) reports
   `never_allocated: true` only when all hold: its first stop cause is none (the provider ended
   the group) or `deadline` (never `allocation_changed`: the provider reported an allocation, so
   the reallocation rules of ADR 0179 and ADR 0221 decide); it carries no `allocation_timeout` or
   `route_deadline` record (those keep their own classes); and its own database shows no machine
   ever placed (no instance latch, no bound allocation), none seen running, and the adapter says
   positively that no custody of any kind began. At least one read must have given a definite instance count and none may have lacked one (a read with no count says nothing about placement). An unknown or raising adapter, or any sighting,
   reports nothing. The safety service passes the marker through only when the reservation's own
   record also shows no sighting (no bound allocation or machine, no run seen, no accrual, no
   uncertain allocation).
2. **Control projects and stores it** as the optional closed key `never_allocated` of the recorded
   final, only for a closed frame and only with one of those causes. Every other stored final
   keeps its shape and digest.
3. **Classification.** `SaladRouteProvider.observe` ends such an attempt as `failed_transient`
   `infra_salad_never_allocated`, only when the stored final says `never_allocated` and nothing
   contradicts it (no recorded exit, route-deadline, allocation-timeout or start-failure record).
   It joins the placement retry classes: one `retry.infra_retries` per attempt, each retry its own
   attempt with its own claim and hold; with none left the stage fails under the same class,
   never `salad_not_passed`. Missing or unknown evidence keeps today's behaviour. A sighting of
   any kind, or custody work, keeps the fail-closed classes of ADR 0221 and the reallocation
   rules untouched.
4. **Settlement.** The attempt settles at the guardian's recorded estimate rounded up (zero when
   it saw nothing run), never above the hold and never caller-supplied, provisional like every
   guardian-bounded settlement (a recorded charge raises or confirms it). Only a final that carries
   the marker may close by the safety service's provider-confirmed reconcile, since a
   provider-ended frame closes that way; `infra_allocation_timeout` and
   `infra_salad_provisioning_overrun` settle exactly as before.
   An estimate above the hold bounds nothing: the attempt stays unknown as before.
5. **Words.** `failure_detail` and the plain report name the token: "the cloud provider never
   gave it a machine before the route ended (nothing ran)", with the retry ending chosen from the
   records.

## Consequences

- A chemistry-style rerun no longer stops on provider capacity; it retries, bounded by the
  existing attempt caps.
- The claim is the guardian's own word plus control's contradiction checks; a receipt reason
  string never selects the class.
- Verified with fakes only; nothing here has run against live Salad.
