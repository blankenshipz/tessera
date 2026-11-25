# ADR 0189: a 150 s execution lease survives control stalls of about two minutes

Status: Implemented. Extends [ADR 0178](0178-lease-renewal-stall-retry.md) (a stalled lease
renewal is retried, not fatal). No schema change.

## Context

ADR 0178 lets the development worker retry a renewal that gets no answer, but only until the held
lease's expiry less the 5 s safety margin. With a 30 s lease renewed every 10 s, that leaves 15 to
25 s of stall margin. A live run showed what happens beyond it: renewals were confirmed every
10 s, then control's request handling stalled for 29 s on a memory-tight host. The lease expired
inside the stall, the next renewal was refused with 409 (expired lease) and the worker killed the
stage (exit 137, `heartbeat_or_renewal_failure`). Long local stages die from this.

Two options were weighed:

- **(a) A longer lease.** Control issues and renews leases for longer; the worker keeps its
  cadence and already works from the expiry control returns.
- **(b) A control-side grace window.** Control accepts a late renewal of an expired lease if the
  same lease and epoch still hold it and nothing was reassigned, and records the late renewal.

Option (b) needs the worker to keep its stage running after its lease has expired, because it
cannot tell a stall from a refusal until control answers. During that time the worker runs on a
lease control already treats as ended: recovery, exit resolution and output custody checks that
wait for "every lease has expired" would see it as ended while the stage still runs. Making those
checks honour the grace window is the same as a longer lease, with more code and a second notion
of expiry. Option (a) keeps one expiry, the one every check already reads.

## Decision

1. **Control's execution lease lasts 150 s** (`tessera_control.execution.LEASE_SECONDS`), at
   acquisition and at each renewal. It is still `min(now + 150 s, hard_expires_at)`.
2. **The worker still renews every 10 s** (`renew_after_seconds`, and at least every 15 ticks).
   A stall that begins just as a renewal falls due (the worst phase) is survived for up to
   150 - 10 - 5 = 135 s; one that begins right after a confirmed renewal, up to 145 s. Keeping the
   10 s cadence, rather than renewing every lease/6, keeps revocation and fencing seen by the
   renewal within 10 s, as before.
3. **Every ADR 0178 rule is unchanged.** A transient renewal failure is retried with backoff until
   the lease's expiry less 5 s; each request gets at most a third of the time left, capped by the
   channel timeout; authority reads and reports are capped by the time left; a refusal (409, 404,
   an authority-gone 403) or a rejected credential stops the stage at once; a stall longer than
   the margin cancels the stage before control's lease expires.
4. **The request-time ceiling stays at 20 s.** `control_timeout_seconds` is still at most 30 s
   less the renewal interval (`REQUEST_WINDOW_SECONDS`), not the longer lease less the interval,
   so one unanswered request still cannot hold up the renewal loop for long.

## What does not change

- **Fencing.** Renewal still requires the same worker, the same lease id and the expected epoch,
  the grant's current epoch, a live grant and credential, and an unexpired lease. An expired
  lease is never renewed or reacquired; a grant still gets one lease. An epoch change
  (reassignment, revocation, admission ownership change) refuses the old holder's next renewal
  with 409 whatever time its lease has left.
- **Hard caps.** `hard_expires_at` (the earliest of grant expiry, worker credential expiry and
  issue time plus the approved task duration) is fixed at acquisition and a renewal can never pass
  it. The supervisor's own duration bound is unchanged. No budget, exposure or concurrency
  ceiling depends on the lease length.
- **Revocation.** Cancellation and revocation move the epoch or grant status, which the next
  renewal (within 10 s) and the authority re-read see, as before.

## Cost

A holder that vanishes without a word (a crashed worker, a lost host) keeps its lease for up to
150 s instead of 30 s, so the paths that wait for every lease of a grant to have expired (exit
without a report, collection checks, the Salad guardian's soft-lease check on input pulls) act up
to two minutes later. This is a liveness delay only: nothing else can take the grant meanwhile,
and revocation is still immediate.

## Not done here

- An operator setting for the lease length. One fixed value keeps the worker's copy
  (`WORKER_LEASE_SECONDS`) and control's in step; a setting can come later with a test that ties
  it to the renewal cadence.
- The cause of the stalls (SQLite write-lock waits on a memory-tight host, see ADR 0178's "Not
  done here").

## Tests

`tests/test_lease_stall_margin.py` runs the worker and control on ADR 0178's shared simulated
timeline with a task approved for an hour: a 110 s stall starting when a renewal falls due (every
control call hangs and uses up its limit) is survived and the next renewal extends control's
lease; the same stall with a 30 s lease stops the stage, as in the live run; a 200 s stall still
cancels the stage before control's lease expires, after holding on for more than 120 s. Control
refuses to renew an expired lease or a lease whose epoch moved on (409 at the API), and a renewal
never passes `hard_expires_at`.
