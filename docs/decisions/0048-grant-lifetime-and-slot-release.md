# 0048 — Grant lifetime, provider slots and release of finished grants

## Status

Implemented. No schema change. Amends the `grant_ttl_seconds` bound of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 2 and adds to its
settlement rules; it leaves the human reconcile routes and the unknown-attempt rules
unchanged, and it is consistent with
[ADR 0046](0046-automatic-not-started-reconciliation.md) (refused launches): see
"Interaction with ADR 0046" below.

## Context

Three gaps showed up on a live run:

1. A stage declared `max_duration_seconds` longer than its grant could live. The grant is
   the run's only authority and the worker stops at its expiry, so the stage could never
   finish. Nothing refused the spec.
2. The grant's lifetime was anchored on the attempt's last update, not on the moment a
   slot was free. Two stages for one worker were both issued grants; the second sat in the
   worker's queue while its clock ran, and a stage that waited for project limits was
   anchored on an old update, so queue time ate the lifetime (a grant could even be issued
   already near expiry).
3. A grant whose run had finished and been confirmed terminal, but that was not
   reconciled before it expired, kept holding a project concurrency slot (and its hold)
   until a person reconciled it.

## Decision

### 1. Admission refuses a grant that cannot outlive its stage

At submit and at approval, for every stage, the provider's grant lifetime (for a worker
stage `grant_ttl_seconds`; a plugin may take it from its own config) must be at least
`max_duration_seconds` plus the settle margin (300 s, the margin the runner already
requires of the approval window). Otherwise the request is refused with 409 and
`grant_shorter_than_stage_duration`, naming the lifetime, the duration, the margin and the
minimum. A provider's `grant_request` must be pure when called with no runner for its
lifetime to be checked here; one that is not is not checked. Stored specs are not re-validated on load, so a campaign already running is not
failed by this rule. To keep long stages expressible, the CPU stage's `grant_ttl_seconds`
upper bound rises from 3600 to 43200 (the Salad bound); defaults and digests of existing
specs are unchanged.

### 2. The lifetime starts when a slot is free

A provider may implement `slot_free(rt, ctx, stage)`. The worker provider's slots belong to
the worker principal: `worker_slots` (default 1, 1 to 8) is how many grants its pollers can
run at once, set by the operator per provider entry (`local_docker` and each `self_hosted`
instance) to the number of pollers sharing the principal. A slot is free while the worker
holds fewer unresolved, unexpired grants than that (an expired one occupies nothing, and a
grant whose launch the worker reported refused occupies nothing either, see below). The runner does not
issue a grant, and so does not start its lifetime, until the project limits **and** the
provider slot allow it; the wait is recorded once per stage as
`campaign.stage_waiting_slot`. When it does issue, it first restarts the grant's anchor at
that moment (only while no `issue_grant` intent exists; once the intent is written the
anchor is fixed so a replay rebuilds the identical request, and a replay neither re-checks
limits nor waits for a slot its own grant may hold). Waiting for limits therefore no longer
shortens the lifetime either. The grant never outlives its snapshot release, which was made
before the wait; if it would expire inside the new lifetime, the release is revoked (once
per attempt, in fresh step slots) and a new one made before the grant is issued. If another
live grant still uses it the issue waits, recorded once as
`campaign.stage_waiting_release`. A release that is no longer active (a person revoked it)
cannot be renewed through the attempt's steps, so the stage is refused with `step_refused`
and the reason, never retried in a loop. A replayed issue (an intent already written)
skips the limits check, so if the store later refuses it the step is recorded as failed
with the store's reason. A provider without `slot_free` (for example Salad, which has its
own exposure gate) is unchanged.

A provider that is not a worker-grant provider keeps its own gate or implements
`slot_free` itself (Salad uses its exposure gate, a plugin may add the method); none of them
inherits the worker slot count.

### 3. A finished grant releases its slot

Each runner tick reconciles an expired, unresolved grant that a campaign issued when, and
only when:

- its one external operation's current reconciliation is a confirmed exit with no
  contradicting observation (`exited`, `termination_confirmed`, contradiction `none`);
- the cost is recorded: `0` for a zero-exposure grant, or the settled cents of the campaign
  attempt that owns it when that figure is within the grant's hold; otherwise the grant
  stays unresolved and nothing is guessed;
- no campaign attempt still owns the grant in flight or as `unknown` (the campaign's own
  flow, or a person, settles those; the sweep never reconciles a grant from under a
  campaign or touches the human path for an unknown effect).

A grant no campaign issued is never released here; single runs stay with a person. It uses
the same accounting as the human route (`reconcile_grant_row`, now shared), is credited to
the campaign's acting principal (its approver, else its submitter, as the runner's own
events and ADR 0046 are) and never to whoever created the grant, and records
`execution.expired_terminal_grant_reconciled` with the observation cursor,
`provider_termination_inferred: false`, `automatic: true` and `credited_to:
campaign_runner`. The scan examines a window of 500 expired grants per tick and carries a
cursor on, so grants that wait for a person cannot starve the ones behind them. It runs in
the campaign runner tick, so a deployment with the runner off releases nothing. A running, unknown,
unconfirmed or contradicted operation, a grant without an operation and a nonzero hold with
no recorded cost are never released.

## Interaction with ADR 0046

The two rules are disjoint by construction and share one accounting step.

- A refused launch (ADR 0046) has no confirmed exit, so the release in section 3 never
  touches it; and an attempt that is `unknown` is never released from under a person.
- ADR 0046 reconciles its zero-exposure grant through the same `reconcile_grant_row`
  as the release and the human route, so ledger handling is identical.
- A grant whose worker reported a launch error is final: control already refuses to renew
  its lease, so `slot_free` stops counting it at once instead of at expiry, and the next
  stage can be issued while the refused attempt waits to be reconciled.
- `not_started` attempts keep not consuming a scientific retry; a grant issued for the
  re-queued attempt gets a fresh lifetime from section 2.

## Consequences

- Specs that could never finish are refused up front with a precise reason.
- Parallel stages for one worker queue before a grant exists, so the lifetime measures
  running time, not waiting time. The default of one slot is conservative; an operator whose
  pollers run several grants at once raises `worker_slots`, and a wrong (too high) value only
  brings back queue time inside the lifetime, never a wrong settlement.
- Finished runs no longer pin project concurrency after expiry.
- Evidence is never invented: the release needs a confirmed terminal observation and a
  recorded cost.

## Tests

`tests/test_grant_lifetime.py`: admission refusal and exact-fit acceptance, the slot wait
and the restarted lifetime (it fails without the anchor restart), no wait when the slot is
free, release of an expired grant with a confirmed exit, and the refusals (not expired,
running or contradicted observation, no operation, campaign-owned attempt, nonzero hold
without a recorded cost, recorded cost above the hold, no campaign owner, credit to the
campaign actor, the scan cursor), a replayed issue that does not wait and the release renewal, the `worker_slots` bounds and file
parsing, the slot count itself, and the slot freed by a refused launch; the store refusing a replayed issue (recorded as
a failed intent and a `step_refused` attempt with the store's reason); and a revoked release
that is still long enough, which also ends as a recorded refusal.
