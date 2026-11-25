# 0181 — A transient provider read failure after start is retried briefly

## Status

Proposed. No schema change. Extends ADR 0174 (pre-start read deadline and idempotent read
retry) to the window after the run began. No spend cap, custody check or admin budget changes.

## Context

A smoke run passed reserve, create, bind, settle, input delivery and start. About a minute
later the guardian's group read timed out, the guardian read that as `provider_unavailable`,
custody went unready and the run was stopped, although the provider was healthy. The retry
added in ADR 0174 applies only while the route is pre-start, unallocated and no custody began;
once inputs were delivered the first transient timeout stopped the run.

## Decision

- After the run began (custody started, an allocation bound, or the instance seen running) a
  transient read failure (a timeout, or `provider_unknown` that is not deterministic) is
  retried at the next poll instead of stopping. Retries are bounded: at most
  `POSTSTART_READ_RETRY_LIMIT` (3) consecutive failures, the third stops, and only within
  `POSTSTART_READ_WINDOW_SECONDS` (45) of the first. The poll backoff is the same bounded one
  (5, 10, 15 s) the pre-start retry uses.
- Each retry is a journaled event with the closed prefix `poststart_read_retry:` and a closed
  reason; the guardian status carries `poststart_read_retries` (count, first/last time, last
  reason, window and limit), additive like `prestart_read_retries`.
- Unchanged stops: a deterministic failure (identity mismatch, group gone), any other error,
  a guardian that is already stopping (budget fence, rate unknown, escalation, an operator
  stop), and the assignment's own deadline, heartbeat and missed-poll clocks, which a retry
  never extends. A run with no custody adapter or no evidence it began keeps the pre-start
  rules.
- Success of any read ends the window; the next failure starts a fresh one.

## Why the spend caps are unchanged

Charging is the safety service's reservation, not the guardian's read: admission counts
the running window plus `UNSEEN_RUN_GAP_SECONDS` (45) of unseen billed time. The retry window is
that same 45 s (asserted at import: `POSTSTART_READ_WINDOW_SECONDS == UNSEEN_RUN_GAP_SECONDS`),
and the instance is treated as possibly running throughout, so the stop can come later than
the first failed read by no more than the margin admission already counts. A sustained failure
still stops the run and custody still goes unready.
