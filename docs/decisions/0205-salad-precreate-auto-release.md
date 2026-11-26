# 0205 — A Salad run that ends before any create was sent retries its reads, then releases its hold and guardian frame on its own

## Status

Accepted. Narrows the Salad live check (ADR 0075), the guardian's lost-create release (ADR 0031 and its
salad-spend-safety contract), the unclaimed zero settlement (ADR 0038 amendment, ADR 0157) and the
start-preflight retry (ADR 0174) by extending it to create. No schema change and no migration.

## Context

A live seed launch failed before the create: one 2 s Salad list read timed out inside the create preflight.
Nothing was sent, no group existed and nothing was spent, yet a person was needed to release the attempt's
5¢ hold and the guardian kept the only open-frame slot for the route window plus 15 minutes.

1. Nothing before the create effect was retried. The registry heartbeat, use-time authority, the guardian
   reserve and status read, and the trusted server's four or five preflight reads all run before
   `RemoteStore.begin` commits the create intent, so a failure there is provably before any effect, the same
   argument ADR 0174 used to retry the start preflight.
2. The honest receipt then blocked the automatic release. The runner turned a not-passed receipt into
   `unknown`, and the provider's claim state (ADR 0157) treated any stored receipt digest as evidence, although
   control's own claim and route records proved nothing was sent. A run that died with no receipt in the same
   state was already unwound at zero.
3. The guardian released a never-bound reservation only past the route deadline plus
   `LOST_CREATE_MARGIN_SECONDS`, or 30 s after a reported clean rejection that followed a `ready`. A reservation
   that never answered `ready` cannot have sent a create, but had no early release, so with the default limit
   of one open frame every later launch was refused at reserve.
4. A create could be refused at the use-time claim after its intent was committed (capacity zero, quota full,
   or a slow but successful preflight older than the claim's 5 s bound). No request was sent, but the route
   carried a create effect, which even the operator's unclaimed reconcile refused.
5. Every such receipt read `custody_stop_or_reconnect_unresolved`, because the stop of a group that was never
   created fails on its missing identity; a guardian `reservation_missing` answer to the live check's cancel
   read as an unrecorded cancel and held the outcome for ten minutes; and an attempt marked `executing` whose
   child never launched waited out the whole window after an executor restart.

## Decision

1. **Pre-create retry.** The live check dispatches create through `salad_precreate.dispatch_create`: after a
   failure it reads the route again, and only when that read succeeds and shows no create or start effect is a
   transient failure retried (a non-deterministic provider read failure, a guardian that did not answer, a
   guardian refusal in a closed transient set including the open-frame limit, a control stall), at most 4
   dispatches with pauses of 2, 4 and 8 s, never past the run deadline. Once any create or start effect exists
   nothing is retried. Retries are counted as `create_preflight_retries` on the receipt, present only when one
   happened.
2. **Guardian silence before `ready`.** A repeated reserve of a reservation that never answered `ready`, is not
   in recovery and is not bound refreshes its heartbeat, so the retry spacing cannot put the frame into
   stop-only recovery. Nothing else changes the silence bound.
3. **Refuse before the intent.** The dispatcher asks the adapter, before it commits a create or start intent,
   whether the use-time claim will refuse the preflight (`remote_execution_salad.preflight_refusal`, the same
   check `claim_proxy` applies). Capacity, quota, a present group or a group that cannot start is refused with
   no effect. A preflight older than 3 s is read again once; still too old, it fails as a `timeout` read
   (`PreflightStale`) with no effect, which the create and start retries treat as transient.
4. **What counts as sent.** A create or start effect is provably unsent when its recorded receipt is the
   trusted server's pre-claim refusal (`provenance: provider`, `proxy_reason: admission_denied`, no provider
   or allocation identity, state `unknown`) and control holds no `salad_proxy.claim` for the operation. The
   claim is committed before any provider call, and `claim_proxy` refuses any claim of an effect that already
   has a receipt. `unclaimed_proof` (operator route and runner alike) accepts such an effect; any other effect
   still refuses as `route_effect`.
5. **Automatic zero release.** When an outcome is recorded for an attempt whose route names no provider
   resource and has no sent create or start effect, with no claim, no manifest and a receipt that did not pass
   and names no provider id, the runner takes the existing unclaimed unwind (cancel route, revoke the grant,
   `reconcile_unclaimed_salad` at 0) instead of `unknown`: `failed_transient` / `t1_salad_unclaimed`, detail
   `live_check_create_not_sent`, settled `unclaimed_zero`, hold released, no person. The same predicate gates
   the existing no-receipt path. For an attempt already `unknown`, the provider's claim state no longer counts
   a stored receipt digest as evidence when that receipt did not pass, names no provider id and has no create
   rejection; `unclaimed_proof` still decides.
6. **Never-ready guardian release.** A never-bound reservation in recovery that never answered `ready`, whose
   fast release was never voided by a group seen under its name, is released 30 s
   (`NEVER_READY_SETTLE_SECONDS`, at least `DISPATCH_CHAIN_SECONDS`) after its last heartbeat, on the guardian's
   own exact-name 404 and complete project list, as `reconciled` with reason `never_ready_absent` (frame verdict
   `absent`) and event `never_ready_absent_reconciled`. The trusted server asks for `ready` before every create
   and `ready` is refused once recovery is set, so no create can follow. The 24 h late-group watch applies as
   for any lost create. Status shows `never_ready_eligible_at` until then.
7. **Honest receipt and cancel.** A run that ended with no create effect has the receipt reason
   `create_not_attempted`, which later stop steps do not overwrite, and no stop is dispatched (the cancel
   already fenced the reservation). A guardian `reservation_missing` answer after control recorded the cancel
   completes the cancel. The executor does not hold an unrecorded-cancel receipt for a route with no create or
   start effect.
8. **Executor resume.** An `executing` attempt resumed with no receipt on disk and no create or start effect on
   its route sends the idempotent no-receipt report once per process; the runner's own proof decides.

## Consequences

- A pre-create brown-out shorter than about 15 s no longer fails the attempt; a longer one ends it at zero
  cost: the runner releases the hold on its next passes and the guardian frees the frame slot about 30 s
  after the last heartbeat, with no person; the runner's retry policy then applies.
- The ordinary lost-create margin, the create-rejected path, the operator `reconcile` and every stop rule are
  unchanged. A create that was claimed, or whose effect has any other receipt, is never released this way.
- Attempts already `unknown` from an earlier build whose only evidence is a not-passed, provider-free receipt
  become eligible for ADR 0157's automatic reconcile on the next runner pass.
- Not covered: the serial executor still does not ask the guardian whether the frame limit is reached before
  `mark_executing`; with the never-ready release and the reserve retry a following launch normally waits less
  than a minute instead of failing.
