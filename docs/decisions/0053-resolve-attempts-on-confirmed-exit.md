# 0053 — Resolving campaign attempts on a worker's confirmed exit

## Status

Implemented. No schema change. Narrows "every unknown attempt needs a person"
([ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 4) for attempts whose
worker has reported a confirmed exit. It sits beside [ADR 0046](0046-automatic-not-started-reconciliation.md)
(refused launches) and is written to coexist with the grant-lifetime change (ADR 0048), which was
merged before this was finished: see "Coordination".

## Context

A worker reports `exited` with `termination_confirmed` when its container is gone. Normally the run
receipt and output manifest follow, the campaign collects and settles, and the grant is reconciled.
When they do not (the receipt report was lost, or an unconfirmed exit observation arrived first and
the attempt went `unknown`), the attempt stayed `admitted`, `running` or `unknown` and its grant kept
holding a worker slot until it expired or a person reconciled it. The reconcile route needs a human
session, so an automated session's call was refused. A real run held both worker slots for about an
hour, and the next launch queued behind it.

## Decision

`campaign_exited_resolve.assess` reads control-plane records only. The runner applies the result in one
transaction (`CampaignRunner._resolve_exited`), re-assessing inside it under the runner lease. It runs
for every runnable campaign on every tick before the campaign is driven, including paused and blocked
ones: it only reduces authority and never starts anything.

### The rule (fail closed)

An attempt is resolved only if **every** guard holds:

- it is `admitted`, `running` or `unknown`, unsettled, with a grant, and its stage's provider
  descriptor is a worker-grant provider that is not billable (a billable or route-based provider
  settles through its own guardian or `settle`, never at zero from a worker observation);
- its grant is unresolved (`active` or `cancel_requested`) and has exactly one external operation;
- the operation's current reconciliation (`current_operation_reconciliation`, which merges worker,
  provider-receipt and recovery observations) is `exited` with termination confirmed and
  `contradiction_state == "none"`. An unconfirmed, running or unknown latest observation, or a later
  adverse one, never qualifies;
- an `admitted` or `running` attempt is left alone while a run receipt exists (the normal flow collects
  it). Until its grant expires it is also left alone while a lease for the grant is unexpired or a
  artifact upload for it is still `uploading` (a small staged upload in `artifact_uploads`, or a large chunked one in
  `snapshot_chunk_uploads` whose manifest names the lease; an expired upload no longer counts) (the worker reports `exited` before it collects,
  uploads and posts the receipt), and for `GRACE_SECONDS` (120) after control recorded the exit (its
  own `recorded_at`, not the worker's clock), so a receipt that is merely late is not overtaken. An
  `unknown` attempt qualifies at once;
- its cost is recorded and never above the hold: a non-billable worker-grant provider costs nothing
  (`local_zero`), which is never more than the grant's `max_exposure_cents`.

An attempt with no observation, or with evidence that does not satisfy the rule, is not touched: it
stays `unknown` for a person. Control never creates an observation.

### What it records and does

In one runner transaction: the grant is reconciled (settlement at most its hold; the project hold moves
to settled in the same transaction, so the slot is free at once); the attempt becomes `failed_transient`
with failure class `exit_confirmed_unresolved` (or, when a run receipt records a non-zero exit code,
`t2_declared_exit_code` or `undeclared_exit_code` exactly as the normal observation classifies it); it
settles with basis `local_zero`; a stage that was `blocked_unknown` returns to `preparing`, so the
campaign unblocks through its normal path and the stage's retry policy decides what follows (a stage
with a single attempt fails instead of hanging). It never becomes `succeeded`: success needs the
collect-and-verify path. The events `execution.exited_grant_reconciled` (observation cursor,
`provider_termination_inferred: false`) and `campaign.attempt_exit_resolved` (`automatic: true`,
`is_evidence: false`, evidence digest) record it, and the campaign receipt shows the attempt with
`exit_resolved`: no pass, no fail, no measurement.

### Idempotence and concurrency

The grant update is guarded on its revision and status, the limit ledger on its held amount, and the
attempt is re-read and re-assessed inside the transaction, so a second sweep, a replay or a concurrent
human reconcile leaves exactly one effect and a repeat returns false. Only the runner that holds the
lease writes; a runner that lost it is refused. If the ledger cannot cover the hold, nothing is written
and a person looks.

## Coordination

- **ADR 0046** handles attempts whose launch was refused (no confirmed exit, zero hold). The rules are
  disjoint: this one needs a confirmed exit, the other forbids any container evidence.
- **Grant lifetime (ADR 0048)** adds a sweep that reconciles an *expired* grant with a confirmed exit
  when no campaign attempt owns it (it deliberately leaves campaign-owned attempts alone). This ADR is
  the campaign-owned counterpart, so the two do not duplicate each other, and both use the same
  evidence test (`exited`, confirmed, contradiction `none`). There is one release path:
  `campaign_exited_resolve.release_grant` is a thin guard (a settlement never above the hold) over
  `execution.reconcile_grant_row`, the accounting step ADR 0048 shares with the human route, so the
  ledger and the grant row are handled identically whichever rule releases the grant. ADR 0048's slot
  accounting (a slot is free while the worker holds fewer unresolved, unexpired grants than
  `worker_slots`) sees the released grant at once.
- The human reconcile route is unchanged and stays human-only.

## Consequences

- A finished stage no longer pins a worker slot until its grant expires; the next launch starts.
- The cost of being wrong is bounded: the evidence is the worker's own trusted confirmation, a
  contradiction blocks it, nothing succeeds without verification, and a transient failure is retried
  under the existing bounds. A worker that confirms an exit it did not observe is the same trust
  assumption as every other observation.
- A confirmed exit whose outputs never arrived is rerun rather than waiting for a person.

## Tests

`tests/test_campaign_exited_resolve.py`: resolution after the grace with the grant released, the ledger
balanced and the events and receipt entry recorded; the grace itself; idempotence under a repeated or
stale sweep; a second runner without the lease; an unknown attempt with no evidence stays unknown and no
observation appears; healing of an unknown attempt after a confirmed exit; each guard (a later
contradicting observation, a settled attempt, a reconciled grant); cost clamped to the hold and a nonzero
hold released and a zero hold with no ledger row; classification of a recorded exit code; a live lease
or an upload in progress (a large one begun through the real route) is never overtaken; the grace counts from control's `recorded_at`; a billable
provider is left alone; the receipt shows `exit_resolved` for every resolution class. `tests/test_campaign_retry.py` is updated: a lost
run receipt after a confirmed exit is now resolved and retried instead of blocking for a person, and a
human reconciliation still unblocks a campaign the runner left alone.
