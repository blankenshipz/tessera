# ADR 0084: client-requested research and review jobs

Status: proposed (schema v73 for the records and routes). Extends the research journey and the
agent plane that [ADR 0033](0033-freeze-over-built-planes.md) froze, as amended by ADR 0082. Builds
on [ADR 0055](0055-mcp-steering-tools.md) (scoped MCP tools), [ADR 0065](0065-operator-enabled-mcp-approval-tool.md)
(flag-gated human tool) and [ADR 0068](0068-sealed-holdout-evaluation-stages.md) (sealed inputs).
Depends on ADR 0082 (Anthropic as a registered model destination, the freeze amendment, and the
destination registry), and refers to ADR 0083 (receipts for every model call) and ADR 0085 (standing
loop and auto-created jobs) by number and by the minimal interfaces below. None of those three is
required for the records and routes in this slice.

## Context

A plan review, a public-dataset scout or a pre-gate review is today done outside Tessera: a person
or an outside coding session reads the plan and writes notes elsewhere. Tessera never sees which
model ran, on which inputs, under which policy or at what cost, and a finding has no recorded
human decision. A client (an agent credential or a person) has no first-class way to ask for such a
job, and no way to read or resolve its findings.

Decided 2026-11-22: Tessera's own agent loop uses Anthropic as a registered model destination, and
the existing research journey is reused rather than replaced. This record reuses the journey for
client-requested jobs (a single reviewer role, a pinned prompt, digest-pinned inputs, a tier from
policy) and adds durable findings, an immutable decision record and the safety invariants (honest
receipts, no self-review, no private data sent anywhere unregistered). Which policy applies to which
trigger, and the automatic creation of jobs when a plan is admitted or advanced, belong to ADR 0085
and are not decided here.

## Decision

### 1. A client-requested job is an ordinary research journey with one reviewer role

A job reuses the research journey rather than adding a campaign container backend or a second
receipt schema. For a client-requested review only, one reviewer role is enough: there is no
separate critic. The planner/critic journey is unchanged, and its independence rules (the critic
is not the planner, and neither is the approver) are not weakened; this slice adds a sibling shape,
not an exception. The single reviewer is still never the submitter of the job (section 5).

A job has a closed kind (`plan_review`, `dataset_scout`, `technique_scout`, `pre_gate_review`), a
title, 1 to 16 inputs, a pinned prompt, a destination, a model tier, a budget and an egress mode.
An input is `{snapshot_id, digest}` and nothing else: the client does not state a data class.
Control verifies that the snapshot exists in the caller's project and that its digest matches, and
derives the data class from the stored record ([ADR 0039](0039-data-class-guard.md); a record with no
class is `private_derived`). The classes are `public`, `private_code_approved` and `private_derived`,
the same names as [ADR 0047](0047-agent-routed-data-destinations.md). The prompt is pinned by its sha256 digest; the prompt text is never stored by
control. The outputs the reviewer produces (a `review.md` and a `findings.json`) go through the
existing custody; control stores the findings rows and the digests it is given.

### 2. Submit and refusals

`POST /projects/{p}/review-jobs` takes a `SubmitReviewJob` body. It may be sent by an agent credential
with the scoped action `review:submit` or by a person. Validation errors are HTTP 422. Policy
refusals are HTTP 409 with a stable `reason` in the body:

| `reason` | Meaning |
| --- | --- |
| `destination_not_enrolled` | the destination id is not registered for review jobs (the default) |
| `tier_exceeds_policy` | the requested tier is above what policy admits for the destination |
| `data_class_not_allowed_for_destination` | an input's class is not admitted for the destination |
| `budget_exceeds_policy` | the budget is above the policy limit (the API also caps it at 10000 cents) |
| `input_sealed` | an input is a holdout partition, declared or claimed; holdout inputs are never review inputs |
| `input_not_found` | an input's snapshot does not exist in the caller's project |
| `input_digest_mismatch` | an input's digest does not match the stored snapshot |

Policy is an injectable object on `app.state.review_policy` with one method:

```
check(destination_id, tier, data_classes, budget_cents) -> str | None
```

It returns `None` when the job is admitted and otherwise one of the reason codes above. The default
policy admits nothing: it returns `destination_not_enrolled`, so a control plane with no destination
registry refuses every submit truthfully. The destination registry (the operator-enrolled model
destination of ADR 0082) is a dependency that is not part of this slice; when it lands it replaces
the default by setting `app.state.review_policy`, and tests inject a stub. The input checks
(`input_not_found`, `input_digest_mismatch`, `input_sealed`) are made by control before the hook, and
the `data_classes` passed to `check` are the derived ones, never client-stated.

### 3. Tier policy and per-job human approval

The tier is one of `haiku`, `sonnet`, `opus`, `fable`. Sonnet or Opus are the defaults, Haiku is for
cheap checks, and a downshift is never silent (ADR 0082, ADR 0083). A job at tier `fable` is created
as `awaiting_approval` and moves to `queued` only when a human calls
`POST /review-jobs/{id}/approve-tier` with the job's `expected_revision` and a rationale. The
approval is a per-job record, human only: an agent credential gets 403, and the reviewer cannot
approve. Every other tier is created `queued`.

### 4. Egress and data classes

In this slice nothing egresses: no runner exists and no model is called. Private inputs
(`private_code_approved` or `private_derived`) are refused unless the policy admits a registered
destination whose registry entry lists the derived class. The default policy, which refuses all,
therefore refuses every private input, and private-input review stays refused until the destination
registry of ADR 0082 lands. This relies on the amendment
recorded in [ADR 0047](0047-agent-routed-data-destinations.md): an operator-registered model
destination is a registered destination, not a public-internet exception.

The job `egress` field is `none` (the default) or `model_runtime`. `none` means no stage egress.
`model_runtime` means only the registered model destination, reached through the model-runtime
allowlist, never a general host. A job with a private input may use only these two values and cannot
ask for anything wider. The model-runtime allowlist is separate from the sandbox and campaign-stage
allowlists ([ADR 0034](0034-governed-sandbox-egress-and-dataset-staging.md)). A survey that needs
public web fetching is a separate allowlisted step; it is never a reason to widen a model call.
Credentials stay references. The reasoning unit must refuse to start if `ANTHROPIC_API_KEY` is set;
that check belongs to the supervisor (ADR 0082) and is not changed here.

### 5. Findings are proposals, not verdicts

After a job's reviewer finishes, the reviewer posts the content of `findings.json` to
`POST /review-jobs/{id}/findings` (scoped action `review:findings`). Control accepts it only for a job in
`queued` or `running`, and only from a credential that is not the job's submitter (a self-submit is
403). Validation is all or nothing: up to 200 findings with unique ids, each with `id`, `severity`
(`info`, `minor`, `major`, `blocking`), `category`, a `claim` of at most 4096 characters, an
`evidence` list (`source` one of `subject`, `context`, `citation`, optional `digest`, `locator`,
`uri`) and a `confidence` from 0 to 1. Optional `review_md_digest` and `receipt_digest` are
recorded. An invalid body is 422 `failed_output_invalid` and nothing is stored. On success the job
becomes `completed` and each finding is stored `open`. All finding text and URIs are untrusted data:
control never fetches a URI, and the MCP tools mark the text as untrusted.

A person resolves a finding with `POST /review-findings/{id}/resolve` (`accept` or `reject`, a
rationale, the finding's `expected_revision` and `expected_digest`). Resolve is human only, through
the same role guard used to promote or decline proposals plus `require_decision_actor`; agents, workers
and the reviewer get 403. Resolve also refuses the principal that submitted the job and the principal
that ingested its findings, even when that principal is a human (403), so one actor cannot both
request and accept a review. Control writes an
immutable decision row (actor, rationale, the finding digest and the job's subject digests) and an
event in one transaction, idempotent on its key. Resolving never changes a plan, proposal, campaign
or gate: every resolve response carries `"advisory": true` and `"execution_authorized": false`. To act
on a finding, a person revises the subject, which is a new digest. A decision on a finding does not
cover `--accept-automation-gate-go` or any other approval.

### 6. Honest receipts and states

States are `awaiting_approval`, `queued`, `running`, `completed`, `failed`, `not_started` (with a
reason) and `cancelled`. Nothing in this slice runs a model. A runner (the research journey, wired
in later) calls the store's `mark_running`, `record_findings` and `mark_failed`; these are
in-process store methods for a trusted worker or reviewer runner, with no HTTP route. `budget_cents`
is a recorded cap, not a reservation, until the runner lands (open question 6). With no runner
attached a job stays `queued` and is never shown as completed, and a provider-disabled path stays
truthful. A job stores a receipt digest only when the runner supplies one; control never invents
one. The receipt fields expected from ADR 0083 are `model`, `tier`, `input_digests`, `tokens`,
`cost_or_usage` and `policy_digest`, with included subscription usage recorded as such rather than as
a dollar figure. Model identity is what was configured or reported, not verified.

### 7. Invariants

- Tenant and project checks run at data access, and the row, event and idempotency record of every
  write are in one transaction.
- Holdout partitions, declared or claimed, are refused as inputs (`input_sealed`); a policy hook may
  widen the check but not admit them. Inputs recorded in artifact lineage as descendants of a
  partition are refused too. Derivations that were never recorded in lineage are not detected.
- Submit, tier approval, findings ingest and resolve are separate authorities; none implies another.
- Prompt text, private reasoning and credentials are never persisted. Only digests, findings and
  metadata are.

## Interfaces and routes

All routes are project-scoped under the control API: `POST/GET /projects/{p}/review-jobs`,
`GET /review-jobs/{id}`, `POST /review-jobs/{id}/approve-tier`, `POST /review-jobs/{id}/findings`,
`GET /projects/{p}/review-findings` (filters `job_id`, `state`), `GET /review-findings/{id}` and
`POST /review-findings/{id}/resolve`. Schema v73 adds `review_jobs`, `review_job_approvals`,
`review_findings` and `review_finding_decisions` (decisions are immutable by trigger). The MCP tools
are described in [docs/mcp-review-jobs.md](../mcp-review-jobs.md).

## Consequences

- A client can submit, list and read review jobs and findings, and a person can accept or reject a
  finding with a recorded actor, rationale and digests.
- Until a destination registry and a runner exist, every submit is refused
  (`destination_not_enrolled`) or stays `queued`. That is the intended truthful default.
- A reviewer that is not the submitter is needed to complete a job; a single credential cannot both
  request and answer.
- The research journey's planner/critic shape and independence rules are unchanged; ADR 0085 decides
  policy per trigger and any automatic job creation.

## Tests

Focused tests cover: submit refusals for each reason code and for 422 validation; the default policy
refusing everything; private inputs refusing egress wider than `model_runtime`; `fable` creating
`awaiting_approval` and approval being human only (agent 403) and stale-revision safe; no model run
and no completion without a runner; findings ingest all or nothing, only for `queued` or `running`,
and self-submit 403; unique-id and size limits; resolve accept and reject, agent, reviewer and
submitter 403, stale digest refusal, idempotent replay, the `advisory` and `execution_authorized`
flags, and decisions immutable; a person who submitted a job being refused on resolve; input
existence, digest match and derived data class (including a missing class as `private_derived`); tenant isolation on every read; and the MCP tools, including the
flag-gated tools being absent by default and the client-side digest check sending nothing on a
mismatch. A schema 72 to 73 migration test covers the new tables. There are no live or paid model
calls in default tests.

## Open questions

1. How the operator-registered destination registry maps an enrolled model destination to the
   admitted tiers and data classes (ADR 0082 owns the registry; this ADR fixes only `check`).
2. Whether a submission should hold a plan's approval until its review finishes, or only report
   it. This ADR only reports.
3. Whether an unlock requirement (in the style of ADR 0049/0051) may later require a `completed`
   review on the current subject digest with no open `blocking` finding.
4. Who may hold a delegated decision label for resolve, and whether it is separate from the campaign
   approval label.
5. How to catch derivations from a holdout partition that were never recorded in artifact lineage
   (`input_sealed` matches declared and claimed partitions and their recorded lineage descendants).
6. Whether `budget_cents` should become a reservation against the project budget once the runner
   lands; it is only a recorded cap now.
7. Pinning the reviewer prompt per kind or per policy, and a literature mirror for scouting private
   plans.
