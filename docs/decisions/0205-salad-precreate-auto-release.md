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

## Amendment (2026-11-26): a create refused at the use-time recheck, after its claim (q-2704)

### Context

A live seed launch ended before any group existed, but neither the attempt nor its 5¢ hold released
itself. The route's only create effect had one receipt from the trusted server: `provenance: provider`,
`proxy_reason: admission_denied`, state `unknown`, no provider id, allocation, request id, response status or
cost. The live receipt read `inconclusive` / `custody_stop_or_reconnect_unresolved` with no stop confirmed.

1. Point 4 described this receipt as a refusal *before* the use-time claim, so it required that no
   `salad_proxy.claim` exist for the operation. That can never hold for this receipt: control accepts a
   provider-provenance receipt only for an effect with a committed claim (`_provider_receipt_allowed`), so the
   receipt always follows a claim. It came from the server's use-time recheck (`claim_proxy(recheck=True)`),
   which runs after the claim is committed and before the one provider call. Point 5's automatic release
   therefore never matched, and the attempt became `unknown` (`observed_unknown`) and the campaign `blocked`.
2. The executor held the outcome for about six minutes (`group_stop_held` / `group_identity_unrecorded`),
   waiting for the guardian to confirm a stop of a group that was never created.
3. No sanctioned recovery fitted. The grant route `reconcile-unclaimed-salad` refuses a claimed create. Even
   where it can release a hold, the runner's blocked pass then settles the attempt `failed_permanent`, which
   ends the stage. ADR 0157's automatic reconcile stops at the per-campaign limit.

### Decision

1. **The server records its own refusal.** When a use-time recheck of a committed create or start claim
   raises, the trusted server records `salad_proxy.claim_refused` (`nonce`, `route_id`, `operation_id`,
   `action`, `stage: use_time_recheck`, `reason: admission_denied`, `provider_call_made: false`). Both rechecks
   come before the provider call, so no call was made for that one-use nonce. `claim_proxy` refuses a nonce
   that was already used and any effect that already has a receipt, so no later request can make a call for
   it either. The record has no public route. It is accepted only for the route's newest create or start
   effect, only while that effect has no receipt and has a committed claim, only from the operation's own
   worker, and only on a store that is composed with the trusted server. It is idempotent per nonce. Writing
   it is best effort: if the write fails, the claim stands (fail closed) and the server's input log records
   `claim_refusal_unrecorded`. A failure after the recheck records nothing.
2. **What counts as claimed.** `salad_unclaimed.create_claimed` ignores a create or start claim when a
   `salad_proxy.claim_refused` exists with the same nonce, route and operation, or (point 6) when an earlier
   build's bare pre-call refusal shows no provider call. Every other claim still counts as a possible
   provider call. Point 4 now reads: the effect's receipt is the server's `admission_denied`
   refusal (unchanged), and the operation has no unrefused claim. Point 5, ADR 0157's proof and the operator
   `unclaimed_proof` all use this one reading.
3. **The executor does not hold a group it never had.** A route that has no recorded `provider_id` and no
   create or start effect that counts as sent (`salad_precreate.sent_launch_effects`) is not held for a
   guardian group stop. The runner's own proof decides. A create that may have been sent, or a recorded
   group, still holds as before.
4. **An infrastructure retry, not a scientific one.** When the driver unwinds an attempt as
   `live_check_create_not_sent` and `retry.infra_retries` has a retry left, the attempt is `failed_transient` /
   `infra_salad_create_not_sent` with detail `live_check_create_not_sent; infrastructure failure, retry N of
   M`, settled at zero. With no infrastructure retry left it keeps `t1_salad_unclaimed`, as before.
5. **Owner-requested unwind of a blocked attempt.** `tessera campaign reconcile-unclaimed-attempts
   --campaign-id ID [--dry-run]` (`GET /campaigns/{id}/unclaimed-attempts`,
   `POST /campaigns/{id}/reconcile-unclaimed-attempts`) uses the owner's human control credential. An agent
   or worker is refused. For each `unknown` attempt it runs the same `campaign_unclaimed.assess` the runner
   uses. For each attempt that assessment accepts, it writes one audited `campaign.unclaimed_reconcile_requested`
   event bound to the verdict's evidence digest. Nothing is settled in that request. On its next pass the
   runner applies its existing unclaimed unwind to exactly those attempts. The only thing the request lifts
   is the per-campaign automatic limit; every guard is re-read in the runner's own transaction. The grant is
   reconciled at zero and its hold released. The attempt becomes `failed_transient` / `not_started`, settled
   `unclaimed_zero`. The stage is re-queued under its own caps, with no scientific retry. A `blocked` campaign
   with no other unknown attempt returns to `running`. The events carry `automatic: false` and
   `requested_by_principal_id`, and such requests do not count toward the automatic limit. A changed
   evidence digest voids the request. A refusal names the closed reason and writes nothing, and a repeat is a
   no-op. This route exists because `reconcile-unclaimed-salad` on its own lets the blocked pass settle the
   attempt `failed_permanent`. It never infers a termination and never charges.

6. **A refusal from a build before the record (q-2704 follow-up).** Builds before v0.103.0 (v0.102.0) ran
   the same use-time recheck but wrote no `salad_proxy.claim_refused`. In that code the trusted server's
   `admission_denied` with status `unknown` comes only from a recheck, which precedes the one provider call;
   the HTTP transport's own `admission_denied` after a create POST (a 201 that did not read settled) names
   `proxy_request_kind`. So `create_claimed` also ignores a claim with no refusal record when all of these
   hold (`salad_unclaimed._unrecorded_refusal`, `salad_precreate.unrecorded_refusal_unsent`): the claim is a
   `create`; the operation records no provider id; the claimed route's newest state (same org and operation)
   records no provider id and no allocation, and its only create or start effect is the claimed one; no
   receipt on the route names a provider id or allocation; and every receipt of that effect is point 4's
   `admission_denied` refusal (provider provenance, state `unknown`, termination not confirmed) with
   `request_id`, `response_status`, `cost_cents`, `provider_rejection`, `proxy_detail`, `proxy_field` and
   `proxy_request_kind` all null and no evidence. Anything else fails closed and the claim counts. The
   unwind settles 0¢, never infers termination and never adds a charge.

   **Known limit (q-2704b review, deferred).** The rule has no build cutoff. Builds from 2026-10-14 until
   `2b632bf5` (2026-11-23) did not record `proxy_request_kind`, so for a claim written by one of those builds
   a transport `admission_denied` after a create POST that Salad did act on could look like this bare
   pre-call refusal. The rule applies to every reader of `create_claimed`, including the runner's automatic
   unwind and the never-sent release below. It is not limited to the owner's
   `reconcile-unclaimed-attempts` request. It can only be reached for an attempt created by such a build
   that is still `unknown`; no attempt created since can have that shape. Before an owner requests the
   recovery of such an attempt, check the provider's own records for a group. Narrowing the rule to the
   owner's request is a deferred follow-up.

### Consequences

- From this build on, a create refused at the use-time recheck releases its hold at 0¢ on the runner's
  next passes, with no person involved, provided the server's refusal record was written. The stage retries
  on an infrastructure retry.
- A claimed create without the server's refusal record stays held exactly as before unless it has point
  6's bare pre-call receipt; one with any other receipt stays held. Only the server writes the record.
- **Attempts already stuck under an earlier build (q-2704 follow-up).** No build before v0.103.0 wrote
  `salad_proxy.claim_refused`, and the record cannot be added later because the effect already has a
  receipt. Point 6 covers the shape those builds left: a create whose only receipt is the bare pre-call
  `admission_denied` refusal, with no group identity and no allocation, now reads never sent, so
  `reconcile-unclaimed-attempts --dry-run` reports it `reconcilable` and the owner's request recovers it at
  0¢ (`failed_transient` / `not_started`, no scientific retry; a `blocked` campaign whose only unknown
  attempt it is returns to `running`). The runner's automatic unwind also takes it while under its limit.
  Any other earlier-build claim (one whose route or receipt shows a provider id, allocation, request id,
  response status, transport diagnostic, charge, evidence or a start) still reads `create_claimed` and is
  refused. Never hand-write the refusal record, invent a termination or a charge, or use
  `reconcile-unclaimed-salad` on its own.
- No schema change and no migration: the new records are events, and `infra_salad_create_not_sent` is a new
  value of the existing failure class.

## Amendment (2026-11-26): a queued route that was never sent is released, not unknown (q-2705 F4, v0.104.0)

**Context.** The executor queues routes behind `max_concurrent_paid_routes`, the guardian's frame limit or
a no-receipt drain without writing anything to control, while the runner issues grants up to the project
concurrency. A queued `route_started` attempt whose silent window passed went straight to `unknown` with
the route still `desired=run`. The unclaimed proof then refused it `route_wants_run`, and no automated
actor could cancel the route (`unwind` is not reachable for an `unknown` attempt), so its 5¢ hold and the
block were permanent.

**Decision.**

1. **Past the silent window, a never-sent route takes the unclaimed unwind.** When the window passes and
   `_never_sent` holds (the route names no provider resource, no create or start effect was sent, control
   holds no create or start claim, and the attempt has no manifest, provider id or passing receipt), the
   driver takes the existing `_abandon` unwind instead of `unknown`: `cancel_route`, `revoke_grant`, then
   `reconcile_unclaimed_salad` on the store's own proof. The attempt becomes `failed_transient` /
   `not_started` with detail `executor_silent_never_sent: ...`, settled 0¢ (`unclaimed_zero`), and the
   stage is requeued under its own caps (an attempt number, no scientific retry).
2. **Already-unknown attempts in that shape.** For an attempt already `unknown` as `executor silent past
   the route window` whose route still wants to run and was provably never sent, the evidence pass (ADR
   0038 amendment, 2026-11-26) issues the reduce-only `cancel_route` when the only unclaimed refusal is
   `claim_state_unknown` / `route_wants_run`, also while the campaign is `blocked`. The unchanged proof
   then decides on the runner's own reconcile.
3. **A live executor's queue is not silence.** A `route_started` attempt whose lease the executor renewed
   after the route started, and which is still live, is not judged silent. The route-window clock in
   effect starts at `mark_executing`.

`unclaimed_proof`, `create_claim_state` and `UNCLAIMABLE_PHASES` are unchanged. Anything with a claim or a
sent effect stays claimed and waits for evidence or a person, as before.
