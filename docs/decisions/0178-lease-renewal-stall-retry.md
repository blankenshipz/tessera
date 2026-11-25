# ADR 0178: a stalled lease renewal is retried, not fatal

Status: Implemented. Narrows ADR 0054 (durable observation delivery) for lease renewals,
authority re-reads, terminal report time limits and the wait for a timed-out request. No schema
change.

## Context

The development worker holds a 30 s execution lease (`tessera_control.execution.LEASE_SECONDS`)
and renews it every 10 s. Before this decision, any exception from the heartbeat or the renewal
cancelled the stage at once (`heartbeat_or_renewal_failure`), and so did any failed authority
re-read in the supervisor's tick (`lease_denied`). Each tick also posted the lease's package
twice (once to refresh the lease, once to validate it), each time with a fresh idempotency key.

On a memory-tight host, local CPU stages that ran for more than about 15 minutes died this way
without being out of memory. Renewals got 200 every 10 s until one renewal got no answer within
the worker's 15 s HTTP timeout. The worker then killed the container, and the next renewal got
409.

Why control's renewal stalled:

- Every control write takes the SQLite database's write lock (`BEGIN IMMEDIATE`), with a 10 s
  busy timeout. The database uses the default rollback journal, so readers and the committing
  writer also wait for each other.
- The renewal checked for a reported launch error (ADR 0046) by scanning every event of the
  project and parsing its JSON, inside the write lock. The only index on `events` is
  `(project_id, cursor)`, and the event log only grows.
- Each per-tick package post adds one `idempotency` row and one `execution.package_prepared`
  event. At about one post per second per worker, the scan gets longer for the whole run. On a
  swapping host, a renewal waiting behind these writes and then scanning could take more than
  15 s.

## Decision

1. **Transient renewal failures are retried.** A heartbeat or renewal that fails transiently
   does not stop the stage while the held lease has more than `LEASE_SAFETY_MARGIN_SECONDS`
   (5 s) left.
   - Transient is classified positively (`is_transient_control_failure`): a
     `ControlChannelError` or `TimeoutError` that `classify_control_failure` calls transient
     (no answer in time, a dropped connection, a 5xx, 408/425/429). The HTTP channel turns every
     transport failure into a `ControlChannelError`. Any other exception, such as a local
     `OSError`, is a local failure and stops at once.
   - The worker retries with backoff (1 s, doubling, capped at 4 s) using the same durable
     renewal key.
   - Each request's time limit is worked out again just before it is sent, separately for the
     heartbeat and the renewal: `min(channel timeout, max(1 s, time left / 3))`, where the time
     left is the lease's expiry less the margin. Two or three attempts fit before the deadline,
     so one hung request cannot use up the window. On the defaults (30 s lease, 10 s cadence,
     15 s timeout) about 15 s are left at a renewal, so a hung renewal takes 5 s, not 15 s, and
     the retry still has time.
   - A request that timed out may still be finishing in the HTTP channel when the retry is
     sent. The retry waits for it out of its own limit (at most `STRAGGLER_WAIT_SECONDS`, 5 s),
     instead of at most half of it as ADR 0054 had, so it is not refused with "control request
     is still in progress" because of an earlier attempt that ends within its limit. There is
     still never more than one request thread.
   - When the lease's expiry less the margin arrives before a renewal is confirmed, the worker
     cancels the stage as before. Every control call the running loop makes is bounded by the
     lease time left (this rule, rule 4 and the report cap of ADR 0054), and a call made after
     the deadline gets at most the 1 s minimum, so the cancellation lands before control's lease
     ends and a lease that has really expired still stops the stage.
2. **Refusals still stop at once.** A refusal (409 fence, 404, an authority-gone 403), a
   rejected credential, or a local failure (the renewal journal) cancels on the first
   occurrence, as before.
3. **The grant result records the retries and names the cause.**
   - `lease_renewal_retries` (failed attempts, stalls a retry got through, attempts still
     unconfirmed, last failure) is added to every outcome of a run that had a failed renewal
     attempt.
   - A stop for this reason carries `renewal_failure`. It says whether control refused the
     renewal (with its HTTP status, and how many unanswered attempts came before it, as in "the
     lease may have expired during the stall") or whether the lease ran out first.
   - The `reason` stays `heartbeat_or_renewal_failure`, so existing readers are unchanged.
4. **Authority re-reads run at a bounded cadence.**
   - A tick reuses the last validated package read of the same lease and epoch for up to
     `authority_reread_seconds` (default 10 s, one renewal interval; 0 to 20). Every renewal
     forces a fresh read.
   - The renewal's own answer updates the lease's expiry, and the expiry never moves earlier: a
     read (fresh or reused) whose expiry is older than the worker's view keeps the later one. A
     failed or stale re-read cannot shorten the worker's view of the lease.
   - Each authority read the development worker makes (the tick's re-read and the read each
     renewal forces) is capped by the lease time left less the margin (`min(channel timeout,
     max(1 s, time left))`), so a hung read returns before the deadline. The supervisor's
     validity check compares the lease's expiry with the same lease clock.
   - A transient failure of an authority read keeps the lease standing only under the same rule
     as rule 1 (time left beyond the margin). A refusal, a changed package or a malformed answer
     still denies it.
   - Revocation and fencing are still seen within one renewal interval, by the renewal itself.
   - `authority_reread_seconds = 0` restores a read on every tick.
5. **The renewal's launch-error check is cheap and exact.** A launch error is only accepted on
   an `unknown` observation with no provider identity (the request model refuses anything else).
   Its `execution_observations` row is written in the same transaction as its event.
   - Control first looks, through the indexed per-grant observations, for such a row.
   - It scans the events only when one exists. A healthy running stage never does.
6. **Report timeouts reach the HTTP channel.** The worker's credential-watching channel proxy
   now passes a per-call timeout to the HTTP channel and exposes its timeout. The existing cap
   on report requests by the lease time left (`REPORT_LEASE_RESERVE_SECONDS`) therefore takes
   effect in deployed workers, not only with a bare channel.
   - Terminal reports are exempt: a confirmed exit, and the egress-journal and image-build
     reports that follow it, always get the channel's full timeout. The stage has stopped, so no
     lease needs protecting. After a cancel at the lease's deadline, or on redelivery against an
     expired lease, a cap by the time left would squeeze them to the 1 s minimum and keep them
     timing out under a slow control, which could age the termination observation past its
     5-minute freshness check (below).
   - A report that does time out does not make the next call fail: that call waits for it out
     of its own limit (rule 1).

## `quarantine_failed` on the same runs

The worker returns `quarantine_failed` only when `DevelopmentCpuRuntime.collect` raises a
`SupervisorError` after a confirmed exit. Control transport errors (`ControlChannelError`,
including a 409) are not `SupervisorError`, so a stalled or refused upload cannot produce it.

The path that matches a control stall is the 5-minute freshness check on the termination
observation (`sealed_outputs.MAX_TERMINATION_OBSERVATION_AGE`):

1. The confirmed exit's report times out during the stall.
2. The run ends `report_pending`.
3. The poller recovers the grant after the delivery backoff (2 s doubling to 300 s).
4. The same durable exit observation, with its original `observed_at`, is delivered.
5. Sealing refuses it as stale: "Fresh matching termination confirmation is required."

The `detail` field of those results names the cause. If it reads as above, it is the same root
cause (the control stall), and this decision removes its trigger. The freshness check is an
output-custody invariant and is deliberately not relaxed here. Any other `detail` (a missing,
oversized or unsafe output) is an independent output problem.

## Not done here

- **WAL mode for the control database.** It would remove the reader and writer waits, but it
  changes backup and compaction behaviour for every deployment. It is left for its own
  decision.
- **A read-only control path for package re-reads.** Re-reads still take the write lock and add
  an event. This decision cuts their rate by about tenfold instead.

## Tests

`tests/test_worker_lease_renewal_stall.py` models the incident: the channel reports the HTTP
channel's 15 s timeout and a request that gets no answer uses up the whole limit it was given,
and the worker and control (`ExecutionStore._now`) run on one simulated timeline. On the
default settings one hung renewal is survived; a lasting outage or disconnect cancels before
control's lease expires; a disconnect while the lease is live is ridden out. A confirmed exit
after a deadline cancel is reported with the full timeout
(`tests/test_cancelled_run_reports_confirmed_exit.py`). The service tests' API channel maps
control's errors as the HTTP channel does (every HTTP error a `ControlChannelError` with its
status, so a 401 or 403 is a rejected credential, not a transport failure).
