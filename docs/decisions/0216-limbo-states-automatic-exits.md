# 0216 — Every limbo state has an automatic, receipt-backed exit

## Status

Accepted. Builds on [ADR 0212](0212-reconciled-uncertain-no-output-retry.md) (re-reading a
blocked attempt), [ADR 0207](0207-salad-runtime-retry-and-lease-window.md) (guardian-bounded
settlement), [ADR 0157](0157-unclaimed-attempt-automatic-reconcile.md) (automatic unclaimed
reconcile) and [ADR 0048](0048-grant-lifetime-and-slot-release.md) (the shared reconcile
accounting). No schema change and no migration.

## Context

Several attempt states could hold a grant and a hold with no clock: nothing moved them on except
a person. `awaiting_charge` waited for a recorded compute charge forever, never blocked the
campaign, and kept siblings from placing holds. The only human way out of an `unknown` attempt
was the grant reconcile, which needs the person to name an amount and always fails the stage.
The automatic zero-spend reconcile was capped at two per campaign, whatever the campaign's size.
A cancel could not finish while any attempt was `unknown`.

## Decision

1. **The guardian's bound.** A Salad attempt's hold is the most its guardian frame may spend: the
   trusted server refuses a create whose budget binding is below the profile's cost cap, and the
   guardian fences the frame at that cap. The hold bounds the cost once the guardian's final says
   the frame is closed (`stopped`, or `reconciled` by the safety service's provider-confirmed
   close), with no orphan, no pending escalation, and no estimate (rounded up) or recorded bill
   above the hold. An uncertain allocation counts only when the provider-confirmed close (or, on
   the person's route of item 3, an operator's attested reconcile) closed the frame. A plain
   `stopped` final with `allocation_uncertain` is not a bound, as strict as the settlement check
   (`_clean_guardian`): that is ADR 0212's case kept for a person.
   `salad_guardian_bound.guardian_bound_refusal` holds these conditions (pure, fail closed). Settling at the hold never under-records. The basis is the provisional
   `guardian_bounded_hold`, so a later charge confirms it or adds the excess (`late_charges`).
2. **Charge deadline.** An attempt in `awaiting_charge` for more than `CHARGE_DEADLINE_SECONDS`
   (3600 s) since a person was first asked to record its charge is handled in one of two ways.
   - If the guardian bounds the cost and control's own read of the guardian record confirms the
     frame closed, the attempt settles at its hold (`guardian_bounded_hold`) with one
     `campaign.salad_charge_deadline_settled` event. A frame closed by the provider-confirmed
     reconcile also qualifies.
   - Otherwise, in one transaction, the attempt goes `unknown` (waiting reason "charge overdue")
     and its stage goes `blocked_unknown`. The campaign blocks with one `campaign.blocked`
     event (reason `charge_overdue`), which records the attempt's previous status. Each blocked
     pass re-reads the attempt. Once a compute charge is recorded, or the guardian's bound
     holds, it returns to `awaiting_charge` with its previous status and its stage resumes
     (`campaign.salad_charge_overdue_resumed`). Holds stay held throughout.
   - Control reads the guardian record for the bound at most once a minute per attempt. A read
     skipped by that throttle returns the last successful one, so it never looks like "no
     bound". An attempt resumed by the bound has its settlement decided in the resume's own
     transaction: the resume event records `settlement_cents` (the hold) and basis
     `guardian_bounded_hold`, and the next settle step applies exactly that without another
     read. It cannot block again: one block and one resume, then settled. The settle step itself
     (grant reconcile, then frame settle) stays the ordinary one, so the grant is never left
     open beside a settled attempt.

   A grant a person already reconciled at 0 with a claimed create gets the same settle-at-bound
   default, but never the block.
3. **Retryable-infrastructure reconcile (human).** `POST /execution-grants/{id}/reconcile-
   retryable-salad` (`tessera campaign reconcile-retryable`) takes only `expected_revision`. The
   caller never supplies the amount. A person may use it only for the grant of one unsettled
   `unknown` Salad attempt whose guardian bounds the cost under item 1, and whose records show no
   output kept: no retained-before-move marker, no manifest or artifact under the grant, and no
   passed, named or retained output on the live receipt. Because a person is acting, an
   operator-attested guardian reconcile also counts as closed here (never for item 2).
   Control's own read of the guardian record is still required (ADR 0041 sections 2 and 7.1):
   neither the executor-recorded final nor an operator's attestation replaces it. The route
   reads the record through the Salad provider's guardian client before its write transaction.
   It refuses, reconciling nothing, when the record reads open, cannot be read, or the process
   has no guardian client. So a frame that may still be running never gets a retry and a second
   hold. The route event records the verdict, sequence and record digest that control read.
   The grant is reconciled at the hold with the shared accounting, and one
   `execution.grant_reconciled_retryable_infra` event is recorded. The blocked runner reads that
   event. It settles the attempt at the grant's settlement and retries the stage within
   `retry.infra_retries` (`failed_transient` / `infra_reconciled_retryable`, never a scientific
   retry). With none left, only that stage fails under the same class. The ordinary reconcile
   route is unchanged.
4. **Scaled zero-spend limit.** `--campaign-unclaimed-autoreconcile-limit` now bounds each stage.
   A campaign may apply it times its stage count, and 0 still disables it. A one-stage campaign
   keeps its old bound. ADR 0046's `MAX_AUTO_REQUEUES` was already per stage (identical
   consecutive repeats of one stage) and is unchanged.
5. **Cancel at the hold.** A cancel no longer stops at an `unknown` attempt that cannot be
   reconciled automatically. The attempt is made reduce-only: a Salad route is cancelled
   (`cancel_route`) and the grant is revoked. Nothing is settled or released. The attempt stays
   `unknown` with its hold held, its stage becomes `blocked_unknown`, one
   `campaign.cancel_held_unknown` event is recorded, and the cancel finishes. A refused
   reduce-only step is recorded once and does not hold the cancel. A cancelling campaign is never
   blocked, and one blocked while cancelling hands back to the cancel. A person can still
   reconcile the attempt's grant afterwards.
6. **Invariant tests.** `tests/test_attempt_state_totality.py` gives each non-final attempt status
   and Salad phase a row naming its automatic exit, its bound (a named constant) and the test
   that drives it. Adding a status or phase without a row fails. The clock rows are driven past
   their bound with a fake clock. `tests/test_salad_outcome_matrix.py` walks provider outcome ×
   guardian final × output kept. Each cell either settles with no person or is on an explicit,
   counted human list that may only shrink.

## Consequences

- Not addressed here (follow-ups): a held unknown attempt of a finished cancel is not settled in
  the campaign record when a person later reconciles its grant (no pass revisits a finished
  campaign), and the outcome matrix pins the human list by an exact count.
- A charge-deadline settlement records the whole hold, which may be more than the run cost
  until a charge confirms it. It never records less.
- The project's ledger releases the hold of a cancelled campaign's unknown attempt only when a
  person reconciles it. The campaign receipt shows the hold as held.
- Still human-only (the matrix's list): a route that never confirmed termination without the
  guardian's provider-confirmed close; a provider-confirmed close of an uncertain allocation
  that kept an output; and a confirmed stop with no guardian final or only an operator's attested
  reconcile, which blocks once at the charge deadline until a charge is recorded. An executor that
  died mid-route still ends `unknown` (no guardian final is recorded); that is a separate change.
