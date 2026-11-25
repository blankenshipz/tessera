# 0157 — Automatic reconcile of unknown attempts whose provider claimed no create

## Status

Implemented. No schema change. Narrows "every unknown effect needs manual reconciliation"
([ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 4 and its unclaimed
Salad amendment) for a second case, next to
[ADR 0046](0046-automatic-not-started-reconciliation.md): an attempt whose provider proves,
from control's own records, that it never claimed a create or start. Reuses the ADR 0048
reconcile accounting ([ADR 0048](0048-grant-lifetime-and-slot-release.md)), the ADR 0041
frames and zero settlement basis ([ADR 0041](0041-execution-frames.md)) and the ADR 0046
`not_started` retry accounting. The hook extends the provider-neutral interface of
[ADR 0014](0014-provider-neutral-execution-interface.md).

## Context

A campaign attempt can become `unknown` (stage `blocked_unknown`, campaign `blocked`) after its
grant was issued and leased but before the provider ever saw a create: for example a leased
grant that expired before the attempt reached its route, or a runner-side unclaimed reconcile
that was refused once. Nothing ran and nothing was spent. A person then had to revoke the
grant and run the operator route `POST /execution-grants/{id}/reconcile-unclaimed-salad`,
which settles it at zero on the proof that control holds no create or start claim. ADR 0046
could never fire for such an attempt: it needs a zero hold, a zero-exposure grant, no
provider (Salad) record and a worker-reported launch error.

For Salad the proof is strong: control writes the `salad_proxy.claim` event in the same
transaction as the intent to call the provider, so no claim means no provider mutation
happened, and a route that no longer wants to run with no create or start effect cannot
start one.

## Decision

### 1. Provider-neutral claim read

The provider interface gains two optional hooks (`campaign_providers.ProviderHooks`):

- `create_claim_state(connection, attempt) -> CreateClaim`: a pure read. `CreateClaim.state`
  is the closed vocabulary `none | claimed | unknown`; `reason` and `proof` are closed
  lowercase codes; `evidence` holds record ids only and is digested into the receipt.
- `mark_unclaimed(connection, attempt_id, stamp)`: the provider's own bookkeeping in the
  transaction that reconciles the attempt (Salad: the record's phase becomes `unclaimed`).

A provider without both hooks is never reconciled automatically (`no_claim_hook`); the
mixin `DefaultHooks` deliberately has neither. A hook that raises or returns anything but a
well-formed `CreateClaim` is an unknown claim state. The built-in worker providers have no
hook and keep ADR 0046 unchanged.

Salad implements the read with the same proof the operator route checks, now one shared
helper (`salad_unclaimed.unclaimed_proof`, which uses `salad_unclaimed.create_claimed`, also
used by the campaign Salad driver): exactly one `kubernetes-job-v1` operation, no create or
start claim for it, and the newest state of each of its routes neither wants to run nor
records a create or start effect. On top of that the attempt's Salad record must exist,
name no provider id, receipt, manifest, evidence or charge, and not be past the route start
(`executing`, `outcome_recorded`, `awaiting_charge`, `settled`, `unclaimed`).

### 2. The rule (fail closed)

`campaign_unclaimed.assess` runs only after ADR 0046's `assess` found nothing, on an attempt
re-read from the database. Every guard must hold:

- the attempt is `unknown`, unsettled, with no exit code, run receipt, manifest, provider
  verdict or evidence digest;
- its grant is in the campaign's project (tenant check at the read) and unresolved;
- no operation of the grant has a provider id; no observation names a provider id, a
  `running` or `exited` state or a confirmed termination; no provider or run receipt, output
  manifest, dispatch, workspace handoff or recovery record exists for it;
- the provider's `create_claim_state` returns `none` with a proof code.

Anything else keeps the human path and records `campaign.unclaimed_autoreconcile_refused`
once per attempt and reason, with a closed `reason` (`attempt_evidence`, `tenant_mismatch`,
`grant_resolved`, `provider_reference`, `provider_observation`, `provider_receipt`,
`no_claim_hook`, `create_claimed`, `claim_state_unknown`, `ledger_inconsistent`,
`revoke_refused`), the provider's own `provider_reason` code and a readable sentence.

### 3. What it does

The runner applies the same reconcile the operator route runs, without weakening that
route (it stays human-only and unchanged):

1. If the grant is still `active`, the runner revokes it through the ordinary reduce-only
   delegated `revoke_grant` step (the step a cancel's unwind uses; same idempotency slot), so
   the operator route's prerequisite, a `cancel_requested` grant with its operations fenced
   and its epoch bumped, holds the same way.
2. In one runner transaction, under the runner lease: the attempt and every guard are
   re-assessed (the evidence digest must be unchanged), the per-campaign bound is re-checked,
   the grant must be `cancel_requested`; the grant is reconciled at zero with the shared
   accounting (`execution.reconcile_grant_row`, which releases its hold; the operator route
   now uses the same function); the provider's `mark_unclaimed` runs; the attempt becomes
   `failed_transient`/`not_started` and settles zero (`unclaimed_zero`, frame closed); the
   stage leaves `blocked_unknown` for `preparing`; and two events are recorded:
   `execution.grant_reconciled_unclaimed` (as the operator route records it, plus
   `automatic: true`) and the receipt event `campaign.attempt_unclaimed_reconciled` (stage,
   attempt, attempt id, grant id, reason `no_create_claimed`, the provider's proof code, the
   evidence digest, settlement 0, released hold, `consumed_scientific_retry: false`,
   `is_evidence: false`).
3. The campaign unblocks as it does when the last unknown attempt is resolved, and the stage
   re-admits under its own caps: like ADR 0046, a `not_started` attempt uses an attempt number
   (at most five per stage) but no scientific retry. It is never evidence for any gate.

A replay finds the attempt settled (or the grant reconciled) and does nothing; the grant
update is guarded by its revision, so the hold is released and the bound counted once.

It runs wherever ADR 0046 does: the running stage's unknown attempt, the blocked campaign's
unknown attempts, and a cancel's unknown attempts.

### 4. Bound

At most `N` automatic unclaimed reconciles per campaign, counted from its
`campaign.attempt_unclaimed_reconciled` events in the applying transaction. `N` is the
service setting `tessera serve --campaign-unclaimed-autoreconcile-limit` (default 2, 0
disables, at most 10; `create_app(campaign_unclaimed_autoreconcile_limit=...)`,
`CampaignRunner(unclaimed_autoreconcile_limit=...)`). Past it the attempt stays `unknown`,
the campaign `blocked`, with `campaign.autoreconcile_stopped` (`kind: unclaimed`, the
readable reason, `limit`, `used`, the evidence digest) recorded once per attempt.

It is an operator setting rather than a spec field: it never authorizes spend (the reconcile
is zero and re-admission stays under the approved caps), and a spec field would change spec
digests.

## Alternatives and fail-closed choices

- A route that still wants to run with no claim is `unknown`, not cancelled automatically:
  cancelling it first would be a further effect on the strength of the absence of a claim.
  It keeps the human path (`claim_state_unknown`, provider reason `route_wants_run`).
- Running the operator route as a delegated step was rejected: it names one vendor in the
  core and its own checks are Salad-only. The provider hook supplies the proof, and the core
  applies the shared accounting.
- A per-stage bound like ADR 0046's identical-repeat count was not used: the request was a
  per-campaign bound, and attempt numbers already bound a stage.

## Consequences

- A session no longer has to reconcile a provably unclaimed attempt by hand; the receipt says
  the platform did it and on what proof.
- A claimed create, any provider reference, observation or receipt, an unknown claim state or
  a provider without the hook still needs a person, and now says why on the timeline.
- New vendors opt in by implementing `create_claim_state` and `mark_unclaimed`.

## Tests

`tests/test_campaign_unclaimed_autoreconcile.py`: the reconcile with receipt, hold release,
unblock and re-admission (blocked and running campaigns); replay; each fail-closed case
(create and start claims, a route that wants to run, a route create effect, a provider id on
the operation or the Salad record, running/exited/terminated/referenced observations, a
provider without the hook, unknown and malformed claim states); the per-campaign bound and a
zero limit; the operator route still human-only and its event unchanged.
