# 0204 — A running frame tolerates a brief price or provider read outage

## Status

Proposed. No schema change. Amends ADR 0031's published-rate rule ("unknown rate blocks new
work" and the safety service's "no grace and no reuse of an earlier rate") for a frame that is
already running, and widens ADR 0181's post-start read retry. The per-run cap, the total
budget cap, the route deadline, heartbeat loss and every identity check are unchanged.

## Context

A running GPU frame was stopped by short provider blips that cost nothing to wait out:

- The safety service re-reads the published GPU rate every 5 s (loop) and on every driver
  heartbeat. A read that failed three fast attempts (about 0.6 s of 5xx) or two timeouts
  (about 4.4 s) set `rate_unknown`, fenced the frame, closed writers and stopped the group.
  The fence is sticky. The same read also ran on a frame that was already stopping, so a blip
  between retained output and the sealed stop tainted a clean pass.
- The guardian stopped a running group as `provider_unavailable` after three consecutive
  failed group/instance reads (ADR 0181). At the service's poll backoff (5, 10, 15 s) that is
  about 15 s of a Salad brown-out, although the 45 s window would have allowed more.

The owner chose to tolerate both for a bounded 60 s window that admission already charges.

## Decision

1. **Stopping frames are left alone.** On the guardian loop and the driver heartbeat, a frame
   that is already stopping (stop-only recovery set, or its guardian assignment no longer
   `armed`: a stop decision, a cancel, or retained output) is neither re-read nor fenced.
   Accounting still runs, so accrual at the last rate continues until the stop is confirmed.
2. **A brief rate outage on a running frame keeps the last rate.** On the same two paths, a
   rate read that gave up on a transient failure (a timeout or `provider_unknown` that is not
   deterministic) keeps the last known rate when the frame is bound, billing has begun
   (`first_running_at` set), a rate is known, the frame is not fenced, and the last good
   observation is under `RATE_STALE_WINDOW_SECONDS` (60 s) old. Accrual and the per-run cap
   check continue at that rate. The frame is fenced, exactly as before, when:
   - the window ends (the next failed read after the last good observation is 60 s old);
   - a fresh read shows a different price (the higher one is kept);
   - required exposure at the last rate (accrued plus the remaining billed window) exceeds
     the per-run cap;
   - the read failed deterministically (stale or mismatched observation, credential refusal,
     any other error), or the frame is not yet running.
   Reserve, bind, `ready` and begin-once stay strict: no tolerance there.
3. **The guardian's post-start read window is 60 s.** `POSTSTART_READ_WINDOW_SECONDS` is 60
   and `POSTSTART_READ_RETRY_LIMIT` is 13 (one failure per 5 s poll over the window plus the
   one that stops), so the window, not the count, ends a brown-out at any poll pace. Identity
   mismatches and deterministic failures still stop at once; a stopping guardian takes no
   retry; deadline, heartbeat and missed-poll clocks are not extended.
4. **The charged gap grows with it.** `UNSEEN_RUN_GAP_SECONDS` is 60 (was 45), still asserted
   equal to the post-start read window and still at least the 44 s worst-case pass it was
   sized for. With more open frames the gap stays `60 + (N - 1) * 16` s.

## Why the spend bound holds

Admission charges the running window plus the unseen-run gap at the admission rate, and the
guardian stops the run at the running window's end regardless of read failures. A failed
group read can delay the guardian seeing the instance (and so the backdated start) by at most
the post-start window, which equals the gap admission now charges, so billed time stays
within what was charged. The per-run hold grows by 15 s of the published rate (at 0.50 USD/h,
about 0.2 cents).

During a stale-rate window the ledger keeps charging the last known rate every second and the
per-run cap check keeps running, so the cap still fences. What the window gives up is seeing a
price increase for at most 60 s; the first good read that shows it fences and keeps the higher
rate. The rate is a published-rate estimate, not the actual bill (ADR 0031), and that estimate
was already up to one 5 s poll stale between reads. The total budget cap, the route deadline
and the hard deadline are not touched.

## Consequences

- A frame that sees a price-endpoint or status-read brown-out of under a minute keeps running;
  a longer one still stops as before (`rate_unknown` or `provider_unavailable`).
- A clean pass is no longer tainted by a rate blip after its output was retained.
- Pre-start read retries (ADR 0174's 300 s deadline) are unchanged; whether a long pre-start
  outage followed by a first running read is fenced by backdated exposure is a separate item.
- Tests cover each tolerance and each fence that still applies. Existing oracles that pinned
  45 s, three polls or a fence on the first failed rate read of a running frame were updated
  to this contract.
