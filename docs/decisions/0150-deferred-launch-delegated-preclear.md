# 0150 — A delegation's review rule clears Salad review stops at a deferred start

## Status

Implemented. No schema change. Amends [ADR 0122](0122-owner-signed-approval-delegations.md)
(a delegated approval "carries no Salad review pre-approval") and reuses the review pre-approval of
[ADR 0044](0044-salad-review-preapproval.md) unchanged.

## Context

A campaign submitted with `not_before` (docs/campaigns.md) is approved now and starts later. A Salad
stage of it still stops at start for a person: the stage's source assessment (a source record minted
for this campaign) and each attempt's plan and source-use reviews (decision requests listed as
`salad_review` in `pending_human_approval`). ADR 0044 lets a person pre-approve those in the approve
body by binding exact digests, but a campaign approved under an owner-signed delegation (ADR 0122) can
carry none, and a delegated approval of a spec with a Salad stage was refused (`spec_needs_human`).
Sessions therefore ran a hand-written watcher that cleared the stops when they appeared: scaffolding
outside the platform, with no receipt naming the authority it used.

## Decision

1. **An optional review rule in the delegation's terms.** `review_rule` names the review `kinds`
   covered (any of `source_assessment`, `plan`, `source_use`), `max_attempt_cents` (the most one
   reviewed stage run may hold; never above the delegation's `per_run_cap_cents`, refused at signing),
   `max_duration_seconds` (the stage's time limit), `provider_classes` (`salad_gpu`, `salad_cpu`) and
   `source_use_intents` (the program outcomes a source may be used for, the source-use review's
   `outcome`). It is part of the closed, immutable terms and of their digest only when present: a
   delegation without it keeps its digest and behaves exactly as before.
2. **The delegated approval.** A spec with a Salad candidate (stage provider, alternate or reroute)
   is still refused `spec_needs_human` when the delegation has no review rule. With one, every Salad
   candidate must sit inside it; otherwise the approval is refused with each reason
   (`review_cap_exceeded`, `review_time_limit_exceeded`, `provider_class_not_allowed`,
   `source_use_intent_not_covered`), recorded as an escalation like any ADR 0122 refusal. A kind the
   rule does not cover is not a refusal: that review stops for a person at run time. The trust,
   data-class and licence-label admission (`_check_references`, ADR 0040) runs as before; a review rule
   admits nothing those refuse.
3. **At the stop, not before.** Nothing is recorded at approval. When the runner first reaches a
   review stop of a Salad stage (after `not_before`, for a deferred campaign) under an approval made
   under a delegation, it re-checks the delegation inside its write transaction: active, unexpired,
   the signer still a live human of the project with the same credential, the review rule present,
   the stage (on its own provider) inside it, the provider still allowed. If at least one kind is
   covered it records an ordinary ADR 0044 review pre-approval row for the stage, bound to the digests
   the runner itself derives from the approved spec and operator profile (the same derivation as
   `tessera campaign preapproval-digests`), with an uncoverable digest for any kind the rule does not
   cover, the stage's own `max_exposure_cents` as the attempt cap (never the rule's or the
   delegation's larger cap), the smaller of the campaign's cap and the delegation's per-run cap as the
   total, and the earlier of the approval's and the delegation's expiry. A
   `campaign.review_preapproval_delegated` event names the delegation id, its terms digest, the kinds
   and the caps. The existing exact-digest resolution then clears the reviews; no new daemon, step or
   table.
4. **Re-checked at every use.** The pre-approval evaluation, in the runner and again in the resolving
   command's transaction, re-checks the delegation for every review it resolves. A delegation revoked
   or expired after the row was recorded, a signer no longer live, or a kind not covered declines
   with stable reason codes (`delegation_revoked`, `delegation_expired`, `creator_not_live`,
   `review_kind_not_covered`, ...).
5. **Fails closed, visibly.** Anything outside the delegation records nothing and leaves the review
   with a person: a `campaign.preapproval_declined` event carries the reason codes, and the
   campaign view's `salad_review` items carry `delegation_declined` (codes only, never an id) for a
   campaign approved under a delegation. An operator profile's `preapprove_reviews: false` still
   disables it.
6. **The receipt names the delegation.** Each `preapprovals` entry recorded under a delegation carries
   `delegation: {id, terms_digest}`; the delegation section of ADR 0122 lists the delegation.

## Consequences

- A deferred Salad campaign approved under a delegation with a covering review rule starts and runs
  through its review stops without a person or a session-side watcher.
- The owner's signed rule is the only authority: it bounds the reviewed run's hold, time, compute and
  source use, and it lapses for every pending and future review the moment it is revoked or expires.
- Caps only narrow: the recorded pre-approval never holds more than the stage's own exposure per
  attempt, the campaign's cap or the delegation's per-run cap in total.

## Addendum: a deferred start must be reachable

A delegated approval now compares the campaign's `not_before` with both the approval's expiry
(`now + deadline_seconds`, as a person's approval does) and the delegation's own expiry. At or past
either, the approval is not recorded: 409 `outside_delegation` with `not_before_after_approval_expiry`
and/or `not_before_after_delegation_expiry`, the campaign stays `submitted` at its normal approval
stop, and one escalation is recorded. The delegation is never extended and no cap changes. At the
deferred start itself an expired or revoked delegation still leaves a review stop in place (above).
Both approval paths use the one comparison `campaign_hold.not_before_reaches`.

## Not done

- A Salad stage placed on an alternate provider is not covered: the recorded pre-approval names the
  stage's own provider only, so an attempt placed elsewhere declines (`wrong_provider`) and stops for a
  person. A stage whose own provider is not Salad (Salad only as an alternate) records nothing
  (`stage_provider_not_salad`).
- Review rule uses are not rows in `delegated_decisions` (its `kind` is limited to approval and gate;
  widening it needs a table rebuild). They are on the pre-approval rows, their uses and the event.
- A delegated approval does not check `not_before` against the approval's expiry as a person's
  approval does; a campaign whose `not_before` is past that expiry never starts (unchanged here).

## Tests

`tests/test_deferred_launch_preclear.py` (the covered deferred start, the receipt, each out-of-scope
refusal, the cap that cannot be signed, no rule, the unchanged digest, restricted data, a kind not
covered, revoked, expired, signer not live, the re-check after recording, the at-stop rule), with the
existing `tests/test_delegation_rules.py`, `tests/test_delegated_decisions.py`,
`tests/test_approval_delegations.py`, `tests/test_campaign_salad_preapproval.py`,
`tests/test_campaign_pending_approval*.py` and `tests/test_campaign_not_before_runner.py`.
