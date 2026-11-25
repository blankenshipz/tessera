# ADR 0188: end-of-attempt control calls ride out a control stall

Status: Implemented. Extends [ADR 0178](0178-lease-renewal-stall-retry.md) (stalled lease
renewals are retried) to the calls a development worker makes after a confirmed exit, and narrows
[ADR 0054](0054-durable-observation-delivery.md) for them. No schema change.

## Context

ADR 0178 let a long local stage survive control stalls while it runs. The calls after the
container has exited still failed on the first stall:

- **The exit report.** It went to the durable outbox and ended the run `report_pending`, and the
  poller redelivered it later. A recovery more than five minutes after the exit then fails to seal
  the outputs: the output seal requires a fresh termination confirmation. The grant ends
  `quarantine_failed` and the attempt closes `exit_confirmed_unresolved`.
- **The output transfer to quarantine.** A `ControlChannelError` (for example the channel's
  "control request is still in progress" after a 22 s stall) ended the run with no result.
- **The run receipt.** It had three tries one second apart, so a 22 s stall used them up.

On a memory-tight host, control stalls of 8 to 22 s recur. A stage that ran for about 50 minutes
was lost this way at its very end.

## Decision

1. **One bounded window, opened at the confirmed exit.** The exit report, the output transfer
   (seal, upload, manifest) and the run receipt each retry a transient control failure inside one
   window of 180 s.
   - **Transient** means what ADR 0178 retries: no answer in time, a straggling request, a lost
     connection, a 5xx, or 408/425/429.
   - **Backoff** is 1 s doubling to 8 s.
   - **The window's bound.** The window is spent when either the backoff slept or the elapsed time
     would pass 180 s, so a hung request cannot stretch it.
   - **Why 180 s.** It stays inside the output seal's five-minute freshness requirement. Control
     accepts output from a run that exited inside its grant even after the lease lapses, so the
     lease is not renewed here.
2. **Retries are idempotent on the control side.**
   - The exit report is the same durable outbox row, re-sent under its fixed key once its own delay
     has passed.
   - The output transfer re-seals the same bytes under the same seal id (a seal never changes) and
     resumes the transfer under deterministic keys. A manifest post that reached control but lost
     its reply is replayed, not duplicated, and no output is lost.
   - The run receipt keeps its one deterministic key.
3. **Refusals are never retried.**
   - **Refusals:** a 409, 404, authority-gone 403 or rejected credential. These fail at once, as
     before.
   - **Local errors** fail at once, as before.
   - **A spent window** fails closed exactly as before. The exit report falls back to the outbox
     (`report_pending`, redelivered by the poller), a transfer failure keeps custody and records no
     result, and a receipt failure is `run_receipt_failed`. Nothing is claimed.
4. **The exited container is never cancelled for a stall.** Before, one failed exit report
   cancelled it and took the outbox path. Now only a refusal or a spent window reaches that path.
5. **The grant result records the retries.** `handoff_retries` lists failed attempts per call
   (`exit_report`, `output_transfer`, `run_receipt`), the last failure, and which call spent the
   window, if one did. It appears beside `lease_renewal_retries`.

## Consequences

- A stall of up to a few minutes at the end of an attempt no longer loses a finished stage.
- A lasting outage still ends the run as before, at most 180 s later.
- The run receipt may now be tried more than three times, always with the same key.
- No control-side change, no schema change.
