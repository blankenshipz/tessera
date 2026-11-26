# 0075 — The executor's worker credential cancels its own routes

## Status

Implemented (no schema or migration change). Amends the cancel credential of the live-check
config in [ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and the executor enrollment of
[ADR 0056](0056-executor-enrollment-into-an-existing-project.md). The public-internet
invariant of [ADR 0047](0047-agent-routed-data-destinations.md) is unchanged.

## Context

The Salad live check cancels a route on failure or at the end of a run. Route `cancel` was
a control command that only a human credential could send, so the live-check config named a
`cancel_credential_file` holding a project `human_session` credential. That credential
lasts at most 24 hours with no renewal path, so a long-lived executor service ends up with
an expired cancel credential. Making it needs a human session, which an agent must not
issue, and in a shared executor project it reaches every route in the project.

## Decision

1. **The worker cancels, nothing else.** `RemoteExecutionStore.worker_cancel` accepts a
   worker-audience credential of kind `worker` and only when its principal is the dispatcher
   worker of the route's admitted operation. It is the same reduce-only stop as a human
   `cancel` (desired `stop`, grant cancellation requested, no resume) and nothing more: it
   cannot pause, approve, start or touch another worker's route. It needs the current route
   revision and proposal digest, is idempotent by key, and a stale pair is a conflict.
2. **Renewal and issuance are the existing ones.** The executor enrollment already issues
   the worker credential and `executor_enrollment renew` renews it, so no credential kind,
   enrollment step or human issuance is added.
3. **Audit.** The route state records `stop_provenance` with `credential_kind: worker`,
   `decision_class: other_credential`, `cancel_authority: executor_worker` and the worker
   principal id; the grant cancellation event is written as before. The live-check receipt
   records `cancel_authority: executor_worker` and `cancel_recorded` (whether the cancel
   was accepted).
4. **Config.** `cancel_credential_file` is removed from `tessera.salad.live-config.v1` and
   `tessera.salad.campaign-executor.v1`; both stay closed, so a stale config naming it is
   refused with the usual closed-config error and must drop the field. `salad_live_setup`
   no longer issues a cancel credential or writes `cancel-token`.

## Consequences

- An operator setup needs no `human_session` token. A leaked executor credential could
  already dispatch its own routes; it now also stops them, which only reduces spend.
- Revoking an executor enrollment removes its routes' only non-human cancel path; revoke
  only after they end.
- Human `cancel`, `pause` and every approval path are unchanged and still need a human.
- Not done: a separate cancel-only credential kind. The worker's own-route scope already
  gives least privilege without a migration.

## Amendment: when the live check cancels, and a cancel that is not recorded

- The live check does not cancel a route whose last post-start observe read `running` for a
  control stall (a control database write lock, a bounded local call that ran out) or a
  transient read failure; it retries at the next interval inside the run deadline and poll
  bound. The guardian keeps its own deadline, budget and heartbeat-silence stops. A lease
  renewal that stalls does not hold back the guardian heartbeat, which re-runs use-time
  authority.
- Every cancel records a closed `cancel_cause` on the receipt; a cancel of a running route
  for any reason other than completion, the guardian's recorded stop cause, the deadline or
  the budget has the reason `running_route_cancelled_<class>`.
- When `worker_cancel` is not recorded, the guardian cancel is still sent, the receipt
  records `cancel_recorded: false`, `cancel_failure` and the reason `cancel_unrecorded`, and
  the run never passes. The executor holds the outcome, for a bounded time, until the
  guardian's confirmed stop of the exact group is reported (ADR 0160).

## Amendment: a control refusal on a running route

- While the last post-start observe read `running`, a `Forbidden` (or an HTTP 401/403)
  cancels the route only when it is a verified revocation: control's readiness probe answers
  ready and the refusal's detail is exactly one of control's authority-gone details
  (`AUTHORITY_GONE_DETAILS` in `tessera_worker/control_channel.py`), never a substring. Any
  other `Forbidden` (a generic `Unauthorized`, an empty or unreadable detail, `Authenticated
  exact Salad route required.`), or any `Forbidden` while control is unready, takes the
  control-stall path above. A refused keepalive keeps the beat from the guardian, so it is
  still tolerated only inside the guardian silence bound. Deadline, budget, a store
  `Conflict` and the guardian's recorded stop cause cancel as before; before `running`
  nothing changes.
- Every refusal leaves bounded evidence: `cancel_cause.refusal` and the receipt's
  `control_refusal` name the driver step, the in-process control call, the HTTP status when
  there was one, control's plain-text detail (anything URL-, path- or token-shaped, or over
  160 characters, is replaced by `unrecognized_detail`), whether it was verified, control's
  readiness, and whether it was skipped. `cancel_cause.class` stays closed.
- Control reads a credential and its latest renewal in one read snapshot, so a renewal
  committed between the two reads can no longer be read half-applied and refused as
  `Unauthorized`; a lock stays an `OperationalError`. A timed-out licence-label read stays a
  `TimeoutError`, not a `Forbidden`.

## Amendment: a control stall past the silence bound on a running route

- While the last post-start observe read `running` and the guardian has recorded no stop
  cause, a keepalive that a control stall keeps from the guardian (a control database lock,
  a bounded local call that ran out, or a refusal while control is unready) is never a
  `silence_bound` cancel, however long it lasts: it is counted in `control_stalls` and the
  beat is retried at the next interval. The guardian keeps its own heartbeat-silence,
  deadline and budget stops, and the run deadline still ends the loop. Before `running`, once
  the guardian recorded a stop cause, for an unverified refusal from a ready control and for a
  transient guardian refusal, the silence bound is unchanged.
- The official control connection waits up to 45 s for a lock (`busy_timeout`, was 10 s).
  The journal mode stays `DELETE`: official backup/restore normalizes snapshots to it and
  rejects `-wal`/`-shm` sidecars, so WAL is not used. Non-blocking compaction and recovery
  connections keep `busy_timeout=0`.

## Amendment: the completion stop is not an execution cancellation

- The live check's own cancel after a verified pass fences the grant and its operation
  (`cancel_requested`) like any cancel. `resolve_attempt` read that fence as a request to
  abandon the run and refused the success ("Execution success cannot override a requested
  cancellation."), so the attempt failed `step_refused` and no output was indexed.
- Control now records an execution success over a `cancel_requested` operation only when its
  own records show the fence is exactly that completion stop: the campaign Salad attempt's
  stored live receipt is `passed` / `verified_journey` with `stop_confirmed`,
  `cancel_recorded` and `cancel_cause.class` exactly `execution_complete`; the exit is
  confirmed; the route's stop provenance is the route's own dispatcher worker
  (`cancel_authority: executor_worker`), the provider stop is confirmed and the route was
  never reallocated; the grant's one cancel transition was that worker's; and the manifest is
  the operation's own, lists exactly the receipt's outputs, is complete, custody-released at
  its digest and retained, with every file's custody row at the listed sha256 and size. Any
  other cancel (a person, an operator grant revoke, a campaign cancel, the deadline, the
  budget, a guardian refusal, an error) or any missing piece still refuses. No receipt status
  is added.
- Attempts an earlier build failed this way are recovered by `POST
  /campaigns/{id}/reconcile-completion-cancels` (`tessera campaign
  reconcile-completion-cancels`; `GET /campaigns/{id}/completion-cancels` is the dry run),
  for the owner's control credential only and only on a finished campaign. It requires the
  exact refusal on a `failed_permanent` / `step_refused` attempt with Salad verdict `ok` and no
  output rows, the same evidence as above, and a re-hash of every output's bytes in custody
  (checked again, by chunk list, in the write transaction). In one transaction it resolves the
  admitted attempt `succeeded` through the same evidence rule, writes the stage's output rows,
  moves the attempt and stage to `succeeded` (the campaign too, with its stop reason cleared,
  once every stage has succeeded) and appends a `campaign.completion_cancel_recovered` event
  sealed by its own digest and bound to the terminal receipt's digest, listing before and
  after states, the evidence and each output's name and sha256. Charges, holds and
  settlements are left as they are. A second run is a no-op success.
- The terminal receipt stays immutable. `resolve_artifact` accepts an output that receipt does
  not list only when a sealed recovery event bound to exactly that receipt's digest lists the
  same stage, attempt, output name and sha256; every other check is unchanged. The
  human-only `reconcile-unclaimed-salad` hold is untouched.

## Amendment: no cancel of a running route on an unexpected or transient error

- A live run lost a running route to a bare `ValueError`: the guardian admin status read the
  live check makes every pass (`SaladLiveSession.evidence`, through `SafetyHTTPSClient.exchange`)
  timed out while the guardian waited on its own provider read, the client turned the timeout
  into `ValueError("service_unavailable")`, and the driver, which did not classify it, ended
  the run as `running_route_cancelled_error` with only the class name on the receipt.
- The admin client now raises `ServiceUnavailable` (still a `ValueError("service_unavailable")`
  for every caller) carrying a closed `detail` (`timeout`, `reset`, `connect`, `tls`,
  `protocol`, `malformed_response`, `non_200`, `oversize`, `identity_mismatch`, `other`). A
  timeout, reset, connect, protocol or malformed-reply failure of the status read is a read
  stall like a timed-out provider read.
- While the last post-start observe read `running` and the guardian has recorded no stop cause,
  only a genuine terminal cause ends the run and sends the driver's cancel: the run deadline, a
  budget refusal, an explicit operator cancel or revocation (a verified authority-gone refusal
  from a ready control, or a control `Conflict` other than a route-revision race), the
  guardian's own decision (its recorded stop cause, or a terminal refusal code it returned, such
  as `stop_only`) or a verified `execution_complete`. Any other error raised in the post-start
  loop (any exception class, a malformed or partial provider or guardian reply, a read timeout,
  a control stall, a guardian that did not answer or whose own request ran out, an unverified
  refusal, a transient guardian refusal past the silence bound) is held: recorded and retried at
  the next interval. The guardian keeps its own silence, deadline and budget stops. Before
  `running` and once the guardian recorded a stop cause, the earlier bounds are unchanged. A
  reply that names no provider status never turns a running route into an unknown one.
- The receipt records the error behind a cancel as `cancel_cause.error`, the latest held error
  as `last_held_error` (with `running_errors_held`), and the latest skipped read as
  `last_skipped_read` (the last two only when present). Each holds the class name, the message
  only when it is short plain text (else `unrecognized_detail`: no path, URL, query, long token
  or secret-looking word survives), the class of the error it was raised from (also a
  suppressed context), the error's closed `detail` token, the raising `module.function` and the
  tagged control call. When the live check exits without a receipt its JSON report carries the
  same `error`, and the executor logs it and any held or cancel error from a receipt. No receipt
  status is added and no schema changes.

## Amendment: the guardian beat is independent of control write locks

The previous amendment kept the live check from cancelling a running route on a control stall,
but the guardian still stopped the group: the beat could only be sent after a registry write, a
lease renewal and a use-time authority check that took the control write lock, so a write lock
of about 30 s silenced the guardian past its 30 s bound. Raising the lock wait to 45 s made
control wait the lock out, but the guardian had already stopped the group.

- **Beat first.** The live check's keepalive sends the guardian beat before any control write;
  the worker-registry write and the lease renewal follow it, each with its own bound, so a slow
  registry write never skips the renewal. Both writes are attempted even when the beat is
  refused, so a refusal the driver holds does not also let the registry or the lease go stale.
  The guardian's 30 s silence bound is unchanged.
- **Authority is a read.** On the heartbeat, the trusted server re-runs use-time authority in a
  deferred read transaction, not `BEGIN IMMEDIATE`, so another writer holding the write lock
  cannot keep the beat waiting. Use-time admission before create and start keeps the write lock.
- **A stalled read, never a refusal, beats on the last verification.** If the authority read
  itself waits out its lock (SQLite busy or locked, not any other database error), the beat is
  sent on the last authority that heartbeat verified, only while the soft lease that
  verification saw is unexpired (never past the hard expiry). The keepalive reports it as a
  control stall with the beat delivered. A refusal (`Conflict` or `Forbidden`) blocks the beat
  and drops the cached verification.
- **No server-side cancel on a launched route.** On a refusal for a route with a start effect,
  the trusted server no longer cancels the guardian or revokes inputs itself; it raises, and the
  live driver classifies (it still cancels on a verified revocation or a control `Conflict`).
  Before launch the earlier best-effort cancel and revoke are unchanged.
- **A lapsed soft lease is not a revocation of a running route.** The soft lease (150 s) is a
  liveness signal that the guardian's own silence bound already enforces. On the heartbeat of a
  launched route, a soft lease that lapsed because renewals stalled is tolerated; the epoch, the
  hard expiry, the grant, the admission state, the worker's readiness, reviews and policy are
  still enforced. The route's own worker may renew such a lease through the trusted
  in-process store (same worker and epoch, grant active, no reported launch error, hard expiry
  not passed); the event records `renewed_after_lapse`. The HTTP renewal route still refuses a
  lapsed lease.
- **Lock waits are bounded by the lock wait, not a timer.** A Python timer cannot interrupt a
  SQLite lock wait. Control connections the keepalive opens wait at most 2 s for a lock
  (`bounded_lock_wait`, which can only lower the 45 s default; journal mode unchanged), and a
  lock is then a stall. A schema or recovery check that waits out a lock now raises that lock
  error instead of reporting a missing schema. Remaining limit: a wall-bound timeout can still
  surface just after a keepalive write returned; both writes use fresh idempotency keys, so that
  only counts one stall and the write is repeated at the next interval. The worker registry's
  60 s liveness bound still applies, so a control write outage much longer than about 50 s
  refuses the next authority read.
- **Receipt.** The live receipt records `guardian_beats`: beats the guardian took, how many went
  out on the last verified authority, and the largest and latest gap between two delivered beats
  (seconds).
- **Service-initiated stop causes.** Every stop the safety service starts records its first
  closed cause, in the transaction that first sets `recovery` on the reservation (a `stop_cause`
  column) and, when the guardian first sets `stopping`, as a guardian `stop_cause:<cause>` event:
  `heartbeat_silence`, `deadline`, `clock_uncertain`, `rate_unknown`, `cost_cap`, `total_cap`,
  `loop_frame_failed`, `guardian_restart`, `control_cancel`, or `stop_only_recovery` for a
  recovery that names no other cause. The first cause wins, and a cancel that arrives after
  another stop (an output already retained, the guardian's own decision) records nothing. The
  live check treats `control_cancel` from its own cancel as no guardian stop of its own, so such
  a receipt still reads `running_route_cancelled_<class>`; an operator cancel the guardian
  recorded as `control_cancel` is likewise never classed `guardian_stop_cause`.
- **Completion cancel recorded as `control_cancel`.** When the live check's own completion
  cancel reaches the guardian before the output is recorded as retained, the guardian's first
  stop cause is `control_cancel`. The custody-seal wait after the provider confirms the stop, and
  the undecided-refusal skip, accept that cause only when `cancel_cause.class` is exactly
  `execution_complete` and this driver's own worker cancel recorded it at control (receipt
  `cancel_recorded_by_driver`; the route did not already say stop). A `control_cancel` from an
  operator, a campaign or an earlier party never gets the wait, any other guardian stop cause
  keeps its word, and a pre-custody reallocation still ends
  `provider_reallocated_before_custody`. When control recorded the cancel and the guardian
  answers `reservation_missing` the cancel is complete (ADR 0205); any other guardian failure
  after control recorded it is retried as an undelivered guardian cancel.
