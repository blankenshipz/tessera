# 0044 — Salad review pre-approval

## Status

Accepted (owner delegate decision: opt-in, default off). Amends
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md): it decides the paragraph
"Pre-approval (recommended, not decided)" and shortens "Still human after A and B" of the
amendment "automatic Salad settlement and unattended GPU campaigns". It narrows nothing in
[ADR 0032](0032-bound-gate-evidence.md) (bound gates, human go),
[ADR 0039](0039-data-class-guard.md) (data class guard) or ADR 0040 (provider trust), and
records two follow-ups for [ADR 0041](0041-execution-frames.md) (execution frames). One
schema migration, appended after the latest one. Default tests use fakes only; no live run is
implied. Standing pre-approval of campaign start is out of scope (a separate record).

## Context

A Salad stage waits on `campaign.stage_awaiting_human` three times: once per stage for the
source assessment (`CampaignSalad.scaffold`, verdict `unverified`), and per attempt for the
`plan` (kind `direction`) and `source_use` (kind `access`) program reviews that
`_propose_decisions` creates with the approving human as `reviewer_principal_id` and
`_await_review` polls. `remote_execution` requires both reviews to be current and effective
for the admitted attempt (ADR 0032). The delegation table (`campaign_delegation.STEP_TABLE`)
deliberately omits `assess_source` and `resolve_decision`.

Everything a human judges in those reviews is fixed by the approved spec, the operator
profile and the admitted snapshot: the outcome, the brief including the compute budget and
wall, the gate, the identity digests, the reason text and the intended use (`_plan_scope`).
What changes per attempt are run-minted ids. So an unattended GPU campaign still stops at
every retry for a human to approve content they already saw at campaign approval.

## Decision

### 0. Opt-in only; a bare approval never implies pre-approval

The owner delegate decided: pre-approval applies only when the human approve body explicitly
carries it, bound to the exact review digests and caps. Approving a campaign digest without a
`preapprovals` entry pre-approves nothing: every Salad review still stops for a human, exactly
as before this ADR. The runner never derives, defaults or infers a pre-approval, and a
submitting agent cannot create one (sections 1 and 3).

An operator Salad profile can only narrow: `preapprove_reviews: false` makes the runner ignore
any approve-body entry for that profile's stages and restores the human stop. `true` (the
profile default) permits an explicit entry and grants nothing by itself.

Within an explicit entry all of these hold, each checked again inside the resolving
transaction:

- bounded by the entry's per-attempt and total cent ceilings (section 4); the entry's
  `max_attempt_cents` must cover the stage's `max_exposure_cents` and its total must stay
  within the campaign's approved budget cap;
- only the exact approved digest, source and provider: a changed spec, source or provider
  is a different digest, the stored row's recomputed core no longer matches, and a fresh
  approval carrying a fresh entry is needed (re-approval supersedes the row; nothing carries
  over);
- provider trust, the ADR 0039 data-class guard and the data-destination policy are not
  touched and still run at their own enforcement points;
- every delegated review is a use row and a receipt entry citing the approval id, with
  decision class `campaign_delegated`;
- over-budget, an unclear billable frame or a guardian disagreement still pauses for a
  human (separate paths this ADR does not relax).

To see exactly what an entry would bind, `tessera campaign preapproval-digests --state-dir D
--salad-campaign-profiles FILE --campaign-id ID` prints, read-only and offline, the three
digests and the ceilings for each Salad stage; paste them into the approve body.

### 1. Opt-in in the human approve body, never in the spec

`ApproveCampaign` gains an optional `preapprovals` list. Absent or empty means today's
behaviour. The spec has no such field, and a spec carrying one fails validation, so a
submitting agent cannot pre-fill it. Only the human principal who approves (the existing
`approve_campaign` rules: role human, `require_decision_actor`, current revision and spec
digest) can create pre-approvals, and only in that approve call.

Each entry:

```
{stage_key, source_assessment_digest, plan_digest, source_use_digest,
 max_attempt_cents, max_total_cents, expires_at?}
```

`stage_key` must name a Salad stage of the spec. `expires_at` is optional and capped at the
approval's `expires_at`; a later value is clamped down, never extended. Duplicate stage keys
are refused.

### 2. Digests over a review core

Each digest is the canonical sha256 (`canonical_digest`) of a **review core**: the scope
the runner would write (the source assessment body, the `plan` scope, the `source_use`
scope) with run-minted ids removed: `study_id`, source ids, study and experiment variant
ids, `split_lock_id`, `basis_digest` and `supersedes_id`. Everything the spec, profile or
admitted snapshot determines stays in: outcome, action, brief (including `compute`
budget, wall and GPU hours), gates, identity `data_digest`, `code_digest` and
`environment_digest`, the reason text template, and `intended_use`. The source assessment
core is the verdict (`supported`), the reason template and the spec's source template.

Control exposes a read-only helper that computes the three digests and their cores for a
submitted campaign, so the human sees exactly what they pre-approve before approving. The
helper grants nothing; a digest only acts once stored with an approval.

### 3. Storage

A new table `campaign_review_preapprovals` (migration appended at the end), one row per
entry, bound to the `campaign_approvals` row. Rows are immutable (a trigger refuses update
and delete). When an approval is superseded by re-approval its pre-approvals are superseded
with it; a re-approval must re-state them, and nothing carries over. A cleanup
(reduce-only) re-approval refuses pre-approvals.

### 4. Delegated resolution

Two new closed delegation steps, valid only for Salad stages:

| step | method and path |
|---|---|
| `assess_source` | `POST /sources/{source_id}/assessment` |
| `resolve_decision` | `POST /interaction/decisions/{decision_id}/resolve` |

The runner issues them only for a stage with an active pre-approval, in place of emitting
`stage_awaiting_human`: `assess_source` at the stage slot (attempt null, ordinal 0) and
`resolve_decision` per attempt (ordinal 0 plan, 1 source use). They run as the approving
human principal through the existing hook, so the decision is attributed `decision_class`
`campaign_delegated`, never `human_session`. The intent's resources carry
`preapproval_id`, which the receipt and events repeat. The rationale is a fixed template
naming the pre-approval id and digest. `resolve_decision` still runs
`validate_program_review` and every existing check in `resolve_decision`.

`admit`, inside the command's transaction, additionally requires for these two steps:
- the pre-approval row exists, belongs to the intent's approval, is not superseded, and
  `now < expires_at`; the approval is `active` and its approver live (existing checks);
- the intent's stage is the pre-approved stage, and the source or decision belongs to that
  stage's study and (for decisions) to the attempt row;
- the core digest recomputed from the **stored** source or `decision_requests.scope_json`
  equals the bound digest;
- `resolve_decision` with `decision` = `approve` only (reject needs no delegation: anyone
  may already reject, and that only reduces); `assess_source` with verdict `supported`
  only;
- the stage's `max_exposure_cents` (the attempt hold) `<= max_attempt_cents`, and the sum of
  the `max_exposure_cents` recorded for each attempt already approved under this
  pre-approval, plus this attempt's, `<= max_total_cents`, read in the same transaction as
  the resolve. This is a ceiling on approved exposure, not on actual spend: a charge above
  a hold is not counted here, and is handled by the existing overrun block and the
  campaign cap, which still bind.

### 5. Fail closed to the human path

Any mismatch, expiry, cap overrun, missing digest, stale basis or refused admit leaves the
review pending, keeps `campaign.stage_awaiting_human` as today and emits one
`campaign.preapproval_declined` event with a reason code (`digest_mismatch`,
`preapproval_expired`, `attempt_cap`, `total_cap`, `approval_inactive`, `wrong_stage`,
`not_approve`). It never fails the stage. A human can always resolve manually, which
pre-empts the delegated step (the decision is then no longer pending).

### 6. What a pre-approval never covers

Gate go, conclusions, exports, publication; data classification and snapshot release
classification; budget bounds, compute charges and project limits; route approval beyond
the existing delegation; code approval (ADR 0039 amendment 1). A source may only be
resolved to `supported`, and a review only approved. ADR 0039 data-class checks and ADR
0040 trust checks are unchanged and still run at their own enforcement points; a
pre-approved review does not make an input admissible.

### 7. Receipt

The v2 campaign receipt gains `preapprovals`: per entry its id, stage, the three digests,
both caps, `expires_at`, and per attempt each delegated resolution (decision or source
id, intent id, digest, hold cents) and each decline with its reason code. A delegated
review is labelled `campaign_delegated`; a recorded approval remains evidence of
authorization, not of execution.

Addendum (agent receipt view): the stored receipt keeps this section unchanged and a person
reads it whole. An external-scope agent credential reads the receipt through
`tessera_control.receipt_agent_view`, which drops each entry's id and three digests and each
resolution's `subject_id` and `review_digest` (with the approver's principal id), and keeps the
stage, caps, expiry and each resolution's kind, attempt, cents and class, so the agent can act
inside the pre-approval without learning who approved it or which request was resolved. The
response is marked `agent_redacted`; its `receipt_digest` names the stored receipt and does not
verify the projection. Platform readers (chaining, gates, unlock, report) read the stored row.

## Deadline fit

Today `_issue_grant` and the snapshot release silently clamp the grant expiry to the
approval and credential windows, so a stage can start with less time than its
`max_duration_seconds`. Instead, before the release, grant issue or route start, a stage
whose remaining window (the minimum of approval expiry, approver and worker credential
expiry, and release bound) is shorter than `max_duration_seconds` plus the settle margin
is refused with the typed refusal code `deadline_too_short`. The stage fails closed: no
grant is issued and no hold is reserved. The clamp stays only as a defensive bound.

The comparison is `remaining < max_duration_seconds + 300` (a remaining window equal to the
requirement passes). It runs on every dispatch of an attempt that has no grant yet, so a
retry attempt is checked against the window left when it is dispatched. It is skipped only
when the attempt's `issue_grant` step already completed (a crash-resume): the grant then
exists under the earlier check and is never longer than the approval.

## Guardian verdict before settling an ambiguous billable frame

ADR 0041 follow-up 6. A billable Salad frame whose outcome is ambiguous (`lost` or
unobserved) is never settled, at any basis, until control has itself read a guardian
verdict for that frame (ADR 0041 §7.1). Without a verdict the frame stays open and keeps
its exposure held; the executor-attested final status is not sufficient for this case.
The human reconcile path is unchanged.

## Invariants and threat model

1. **No self-approval.** Pre-approvals exist only in the human approve body; the spec,
   submit and every agent or worker credential cannot create them. The delegated actor is
   the approving human, checked live per call.
2. **Spec drift is impossible.** The approval pins the spec digest, and the digests cover
   spec- and profile-derived content. A changed spec needs a new submission and approval;
   a changed profile or snapshot changes the core and is declined.
3. **TOCTOU.** The digest is recomputed from the stored row inside the resolving
   transaction, not from what the runner built.
4. **Replay and idempotency.** One intent per (approval, stage, attempt, step, ordinal); a
   replay returns the recorded response and consumes no cap twice.
5. **Stale basis.** `basis_digest` is outside the digest but `resolve_decision` and
   `_await_review` still require a current, effective review; a stale one is abandoned as
   today.
6. **Retries.** Each attempt needs its own match and consumes `max_total_cents`; caps are
   read and written with the resolve, so concurrent attempts cannot both fit.
7. **Lapse.** Cancel, supersession, lapse of the approval or approver credential, or
   pre-approval expiry stops delegation at the next admit.
8. **Malicious spec author.** It can choose content the human then sees in the helper; it
   cannot change it after approval, raise caps, pick the verdict, or reach steps outside
   the table. A misleading brief is a human-review risk, as without pre-approval.

## Consequences

An approved Salad campaign can run its retries unattended within the pre-approved caps, and
the receipt shows which reviews were delegated. A human no longer looks at each retry,
which is the accepted cost; the caps and the expiry bound it. Costs: one table and
migration, two delegation steps, a helper endpoint, a new refusal code and receipt field.

**Still human after this ADR.** Campaign approval and the choice to pre-approve;
staging the input (public declaration and code approval); any review that does not match
its digest, exceeds a cap or comes after expiry; reject is always available; the invoice
charge; limits, enrollment, guardian and custody deployment, orphan, blocked and ambiguous
frame reconciles; go, conclusions, exports and publication.

## Revisit when

A provider other than Salad needs review pre-approval; an owner wants per-attempt caps that
differ from the stage hold; or the ADR 0041 per-frame guardian record and fence land (the verdict check itself is
already in `settle_frame`, for the automatic billable bases only).
