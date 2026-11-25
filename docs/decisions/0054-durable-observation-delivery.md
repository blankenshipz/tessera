# 0054 — Durable, retried delivery of worker observations

## Status

Accepted and implemented (2026-11-21). No schema change on the control plane. Narrows the
"cannot be confirmed" rule of the development worker (see
[external operation journal](../external-operation-journal.md)) for **observation reports**; it
does not change heartbeat, renewal or run-receipt reporting. It sits beside
[ADR 0046](0046-automatic-not-started-reconciliation.md) (refused launches),
[ADR 0048](0048-grant-lifetime-and-slot-release.md) (release of finished grants) and the control-side
resolution of attempts whose exit control has recorded.

## Context

On a live run a stage container exited (137). The worker's `exited` observation could not be
delivered once, and was never delivered. The attempt stayed `running`, held its grant and a
worker slot, and needed a person to cancel it.

The cause was in the worker, not control. The worker already kept every observation in a durable
journal with a fixed idempotency key, but a failed delivery ended the run with a *recorded*
outcome (`observation_report_failure`). The poller recovers a grant only while it has no recorded
result, so the pending observation was never retried, and the process that could have delivered
it never looked at it again.

## Decision

1. **The journal is an outbox.** Each observation (and egress journal) is a row in the runtime's
   `reports` journal with a stable idempotency key derived from its content, and now an attempt
   count, a first-failure time, a `next_attempt_at`, the last bounded error class and an
   `abandoned` flag. A row is `completed` only when control accepted it.
2. **A failed delivery is not a verdict, and a slow report never stops a healthy container.**
   A delivery that fails for a transient reason (a timeout or other connection error, 5xx,
   408/425/429) is recorded in the outbox and retried after the backoff.
   - **While the container is observed `running`, nothing is cancelled.** The run keeps going and
     the next ticks retry the report within the lease. Authority is proven by the lease renewals,
     which still cancel the container when the lease or authority is lost. (A live run was killed
     twice by the worker itself: a routine `running` report took 7 s against a fixed 5 s timeout,
     was treated as a failed report, and the healthy container was cancelled with exit 137.)
   - **Cancellation remains for:** a confirmed refusal (a 4xx control understood, such as a
     revoked or reconciled grant), a rejected credential (the observation then waits for the
     renewed credential), lost renewals, and any report that fails after the run has ended. The
     last case still ends the run as `report_pending` with **no recorded result**: the poller
     recovers the grant after the backoff, re-observes the container, and delivers the same
     observation, whatever state the container reached in between.
3. **Bounded backoff that survives a restart.** The delay is 2 s, doubling per attempt, capped at
   300 s. `next_attempt_at` is persisted, so a restarted worker (or a crash loop) does not resend
   early and does not hammer control. The poller keeps only an in-memory shadow of it.
4. **Idempotent delivery.** The observation's key is fixed for its content, so a retry after a
   lost reply is a replay of the same request: control already answers a repeated key with the
   stored response and records the observation once. A retried observation may arrive after the
   lease expired; control accepts observations until the grant is reconciled (it fences authority,
   not forensic evidence).
5. **Once per exit.** Control's answer to an accepted confirmed exit is stored with its row. A
   resumed run (for example when only the exit's egress journal could not be delivered, or a
   collection failed and the run is retried) reuses that answer instead of sending a second exit
   observation, so control records one confirmed exit per operation.
6. **Order.** A confirmed exit supersedes any earlier undelivered non-exit observation of the same
   operation (marked abandoned, never sent), so control is never shown an old `running` after the
   exit.
7. **Stop conditions.** A permanent refusal (an HTTP 4xx control understood and refused, such as
   an already-reconciled grant) is recorded once as an `unknown` outcome (`observation_refused`),
   never retried. An operation whose reports have been undeliverable for 24 hours, counted from
   its earliest undelivered failure (a superseding exit does not restart the clock), is abandoned
   the same way. Both leave
   the container evidence and workspace intact for a person.
8. **Never invented.** The worker only ever sends an observation it made. When an exit is never
   delivered, nothing on control's side is fabricated: the existing expiry path applies unchanged.
   Before the grant expires the runner waits; at expiry it marks the attempt `unknown`
   (`observed_unknown`) for a person, with no observation, receipt or success recorded. If the
   exit is delivered late, while the grant is still unreconciled, the control-side resolution of
   confirmed exits then applies as for any other exit.

## Control timeout and the lease

The worker's control-channel HTTP timeout is an operator setting, `control_timeout_seconds`
(1 to 20 s, default 15 s; it was a fixed 5 s): a key of the poller configuration file, the
`--control-timeout-seconds` flag of `development-poll` and `development-run`, and a field of the
development worker configuration. A value outside the range refuses the configuration. It is only
the time one request may take; a request that exceeds it is a transient failure handled as above,
never a verdict on the run.

The range follows the lease. A worker lease lasts 30 s and the single-threaded loop renews it every
10 s, so a request that hangs for the whole timeout must still leave the lease alive: the ceiling is
the lease minus the renewal interval (20 s), and a slower renewal interval lowers it
(`renew_after_seconds` 15 gives 15 s). Independently, each observation report's request is capped by
the time left on the lease minus one renewal interval (never below 1 s), so a report cannot by
itself outlast the lease.

A request that times out leaves its transport thread finishing briefly (the socket timeout restarts
on every read). The channel's next call (usually the heartbeat) waits for it instead of being
refused with "still in progress", which would have cancelled the healthy run as a failed heartbeat.
The wait is at most 5 s and at most that call's own timeout (half of it before ADR 0178), so a call
with a short deadline (a cancellation path) is never held past it; it comes out of the call's own
deadline; and there is still only ever one request thread.

## Open

A single slow or failed heartbeat or renewal still cancels the run at once, even when the lease has
time left (the loop does not retry a renewal inside the lease). That is existing behavior, not part
of this decision; whether a failed renewal should be retried while the lease is still live is a
separate follow-up.

## Consequences

- A transient control outage no longer strands an attempt as `running`; its exit is delivered
  once control is reachable again, within the grant's life and the 24-hour bound.
- A grant whose delivery is pending is not re-run: recovery reconciles the existing operation and
  never starts a second one.
- The poller's counters treat a pending delivery as neither a grant result nor an error. The
  single-run `development-run` exits with code 4 (`EXIT_REPORT_PENDING`) while an observation
  waits, which the worker daemon retries with backoff like any failure; a rejected credential
  still exits 3, with the observation waiting for the renewed credential.
- A worker that is gone for good, or a grant reconciled by a person first, ends with the
  recorded refusal or the aged-out outcome rather than retrying forever.
- Heartbeat, renewal, package-refresh and run-receipt reporting keep their own idempotent
  journals and their existing outcomes.

## Tests

`tests/test_worker_observation_outbox.py` reproduces both live failures (a slow or timed-out
`running` report that must not cancel the container and is redelivered, a confirmed refusal that
still cancels, a lost lease that still cancels, and the configurable timeout) and the original one (the confirmed-exit report
fails once, is delivered by the next cycle, once, under the same key), the persisted backoff and
its cap across a restart, a lost reply replayed with one stored observation, supersession of a
stale `running`, a permanent refusal and a 24-hour age-out (neither retried), a late exit
accepted after lease expiry, the poller's retry window, and the unchanged expiry resolution of a
never-delivered exit.
