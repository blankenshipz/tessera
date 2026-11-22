# ADR 0086: hold an exited attempt until its output registers, and do not mark it late

Status: accepted. Amends ADR 0064 (hold an exited attempt while outputs upload), ADR 0053 and the
`late` rule of ADR 0050. ADR 0047's public-internet invariant is untouched: nothing here changes
how a worker reaches control or where an output may be fetched from.

## Context

A worker reports `exited` first, then collects, hashes and registers its upload (a gigabyte takes
a while), transfers it, and posts the output manifest and the run receipt. Two things broke that
sequence once the grant had expired.

1. **Nothing is registered yet.** ADR 0064 holds an attempt while an open upload gains bytes, but
   ADR 0053's 120 s grace after the exit applied only to an unexpired grant. For an expired grant
   the first runner tick after the exit resolved the attempt as `exit_confirmed_unresolved`,
   reconciled its grant and freed the slot before the upload could register.
2. **Expiry marked the output late.** `ArtifactStore._late` set `late = 1` on any artifact or
   manifest that arrived after the grant or lease expiry, even while the attempt was held open.
   Late manifests are refused when a result is committed or accepted (`result_checkpoints.py`, `result_acceptances.py`), so a
   held attempt still could not yield a usable receipt.

From the code (no live logs were available, so this is derived, not observed): the repeated
`/complete` 201 answers are a caller rerunning the whole upload set after a later step fails,
because a replayed `/complete` is answered from the idempotency record. The step that cannot
succeed once the grant has been reconciled is the run-receipt post (refused when the grant is no
longer `active` or `cancel_requested`).

## Decision

1. An `admitted` or `running` attempt with a confirmed exit and no manifest is not resolved within
   `GRACE_SECONDS` (120 s) of the exit control recorded, whether or not its grant has expired. The
   runner's own observation applies the same rule, so it does not mark the attempt `unknown`
   either. While that grace runs, open uploads are already observed, so their stall window starts
   at first sight. After the grace, ADR 0064 applies unchanged: an open upload gaining bytes holds
   it (10 minute stall window, 2 hour ceiling after the exit, approval window), and a landed
   upload holds it for the grace. An `unknown` attempt keeps ADR 0064's behaviour.
2. **Lateness is judged against the run, not the clock.** An artifact or manifest is not late for
   grant or lease expiry when control recorded a confirmed exit for that lease and epoch before the
   expiry, that exit is still the latest observation, the grant is still `active`, and less than
   `MAX_EXTENDED_HOLD_SECONDS` have passed since the exit. Every other check in `_late` is
   unchanged: grant status, grant and lease epoch, and the task's revision, approval and plan
   digest. A resolved, reconciled or cancelled grant is still late, so an output that lost its
   hold is never accepted as on time.
3. An output that never registers is still resolved at zero cost after the grace.
4. **Scope of the not-late rule.** Decision 2 is a property of the grant and the run, not of a
   campaign: it applies to every grant, whether or not a campaign runner is holding an attempt.
   It is bounded by `MAX_EXTENDED_HOLD_SECONDS` (`artifact_limits.py`, shared with ADR 0064's hold)
   from the exit control recorded, and it does not use the stall window or the approval window,
   which only the campaign hold knows. A grant nobody resolves therefore accepts a run's outputs
   as on time for up to two hours after its exit, and no longer; grants that need the narrower
   campaign bounds are resolved by the runner, which releases the grant and makes later output
   late.
5. **An inconsistent exit report earns no receipt.** The exit observation comes from the worker,
   and so does a receipt's `finished_at`, so neither proves when the container stopped. The
   run-receipt endpoint therefore refuses a receipt in two cases, and states what each one proves:
   - its `finished_at` is later than the confirmed exit control recorded for that lease and epoch
     (beyond the existing clock-skew allowance): the worker contradicts itself;
   - control's own reconciliation of the operation shows a later observation that the container
     was running or unknown again (`same_time_contradiction`, `later_adverse_observation`): the
     exit report cannot have been the end of the run.

   A worker that reports a consistent early exit, a `finished_at` at or before it, and no later
   observation passes both checks, and nothing control holds authenticates the time a container
   really stopped. That case is bounded by decision 6 instead of being trusted.
6. **An output must register within the grace to be on time.** The not-late rule of decision 2
   applies to an artifact only if its upload began (control's `created_at` of the upload, or
   the arrival of a single-request upload) no later than `OUTPUT_REGISTRATION_GRACE_SECONDS`
   (120 s, shared with the resolver's grace) after the exit control recorded. Registration time
   is control's clock, not the worker's word, and an upload declares its content digest when it
   begins, so the bytes are fixed at registration. A worker that reports `exited` early and keeps
   computing can therefore gain at most the grace: output it registers later is late whenever
   the grant had expired, and a manifest is late if any of its artifacts is. This enforces, with
   control's own time, what the receipt checks of decision 5 can only cross-check.

## Consequences

- A registered upload that finishes minutes after the grant expired is stored on time, its
  manifest attaches to the attempt with the run receipt, and the attempt succeeds
  (`tests/test_large_output_hold.py`, which fails without this change).
- For an unexpired grant, an attempt that has a manifest but no receipt is now held by the "an
  artifact landed in the last 120 s" rule of ADR 0064 instead of a separate 120 s timer from the
  exit; the two are close, but this change is not separately tested.
- An open upload whose row has expired still holds the attempt for up to 120 s after the exit.
  Tests of that case assess after the grace.
- The 120 s grace is applied before the approval-window check in the resolver, so an attempt
  can be held up to 120 s past the approval window; it is bounded and not separately tested.
- A later `unknown` observation after the exit refuses the receipt even for an honest worker
  (fail closed; the attempt is then resolved as unresolved at zero cost). A recovery reconciliation
  fences the original worker before the reconciliation is read, so that source is refused by the
  fence rather than by decision 5.
- A worker whose clock runs ahead of control's by more than the clock-skew allowance
  (`MAX_OBSERVATION_FUTURE_SKEW`, 5 s) is refused a receipt for a run that finished after the exit
  control recorded, the same way the endpoint already refuses a future `finished_at`.
- A worker needing longer than 120 s between its exit and registering its upload is still
  resolved, and (decision 6) its late output is marked late. A worker-side "registering" signal sent with the exit report would remove that timing
  dependence; it is a follow-up.
