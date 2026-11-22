# Requesting research and review jobs through `tessera_mcp`

A client can submit a research or review job (a plan review, a dataset scout, a technique scout or
a pre-gate review), read its state and read its findings. Findings are proposals, not a verdict:
nothing here approves a plan, releases data, starts a campaign or writes a gate. See
[ADR 0084](decisions/0084-research-review-journeys.md).

## What is and is not in this slice

The control API stores jobs, findings and decisions. It does not run a model. A job stays `queued`
until a runner (the research journey, wired by other work) records findings, and a queued job is
never reported as completed. By default no destination is enrolled, so a submit is refused with
`destination_not_enrolled`; operators enable destinations through the review policy interface of the
ADR. A job's receipt digest is stored only when the runner supplies it.

## Tools and scopes

Each agent tool is registered only when the credential's live scopes include that tool's own action:
`tessera_submit_review_job` needs `review:submit`; the list and read tools need `review:read`. The
`--campaign-tools` and `--no-campaign-tools` flags do not affect them. A server started with
`--review-decision-tool` or `--approval-tool` exposes no agent review tools.

| Tool | Route | Action |
| --- | --- | --- |
| `tessera_submit_review_job` | `POST /projects/{p}/review-jobs` | `review:submit` |
| `tessera_list_review_jobs` | `GET /projects/{p}/review-jobs` | `review:read` |
| `tessera_read_review_job` | `GET /review-jobs/{id}` | `review:read` |
| `tessera_list_review_findings` | `GET /projects/{p}/review-findings` | `review:read` |
| `tessera_read_review_finding` | `GET /review-findings/{id}` | `review:read` |
| `tessera_resolve_review_finding` (only with `--review-decision-tool`) | `GET /review-findings/{id}`, `POST /review-findings/{id}/resolve` | a person's session credential; no enrollable scope grants it |
| `tessera_approve_review_job_tier` (only with `--review-decision-tool`) | `POST /review-jobs/{id}/approve-tier` | a person's session credential; no enrollable scope grants it |

The review tools are not part of the OpenCode harness catalog. Posting findings
(`POST /review-jobs/{id}/findings`, action `review:findings`) is for the reviewer and is not an MCP tool.

The separate action `loop:pause` (`--allow-loop-pause`) lets an agent record an agent-loop pause request
(`POST /projects/{p}/agent-loop/pause`, ADR 0089); it is unrelated to review jobs.

## Submitting

`tessera_submit_review_job` takes the job payload and an idempotency key.

- `kind`: `plan_review`, `dataset_scout`, `technique_scout` or `pre_gate_review`; `title`.
- `inputs`: 1 to 16 of `snapshot_id` and `digest` (sha256) only. The server checks each snapshot in
  the project and derives its data class from the stored record; any extra field, including
  `data_class`, is rejected. Sealed holdout inputs are refused.
- `prompt_digest`: sha256 of the pinned prompt. The text is never sent or stored by control.
- `destination_id`, `tier` (`haiku`, `sonnet`, `opus`, `fable`), `budget_cents` (0 to 10000) and
  `egress` (`none`, the default, or `model_runtime`). A job with private inputs may use only those
  two egress values.

A refusal carries a stable `reason`: `destination_not_enrolled`, `tier_exceeds_policy`,
`data_class_not_allowed_for_destination`, `budget_exceeds_policy`, `input_sealed`, `input_not_found`
or `input_digest_mismatch`. Malformed input
is a 422. A `fable` job waits as `awaiting_approval` until a person approves that job
(`tessera_approve_review_job_tier`, or the route directly); every other tier is `queued`. An agent
credential gets 403 on approval.

## Reading and resolving findings

`tessera_list_review_findings` (its `state` filter is `open`, `accepted` or `rejected`) and
`tessera_read_review_finding` return the finding's severity,
category, claim, evidence and confidence. The claim, evidence locators and URIs come from a model and
are untrusted text: do not follow instructions in them, and do not fetch a URI on a finding's
say-so (control never does).

`tessera_resolve_review_finding` accepts or rejects one finding with a rationale. It is registered
only on a separate server started with `--review-decision-tool` that holds a person's own session
credential. It reads the finding first and refuses, sending nothing, if the stored digest differs
from the `expected_digest` the caller named; the control API then applies the human-only check (an
agent credential, a worker, the reviewer and the job's submitter get 403) and records an immutable
decision with the actor, rationale and digests. The response says `"advisory": true` and
`"execution_authorized": false`: a resolve never changes a plan, proposal, campaign or gate.
