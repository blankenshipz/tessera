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
agent credential, a worker and the reviewer get 403; see "Who may resolve" below for the person separation) and records an immutable
decision with the actor, rationale and digests. The response says `"advisory": true` and
`"execution_authorized": false`: a resolve never changes a plan, proposal, campaign or gate.

### Who may resolve

By default resolve is refused (403, reason `same_person`) for the same person as the job's submitter
or as whoever posted the findings, and posting findings (and the runner's state calls) is refused for a
credential that shares a root with the job's submitter. A person is an enrolment root: control walks the
credential chain to the principals with no creating principal, and callers with intersecting roots are the same
person. A chain deeper than the walk's bound fails closed. Control cannot verify that two roots are two humans.

Only the owner issues agent credentials, so every agent and session the owner creates shares the owner's root. A
second person is enrolled by the owner ([people-enrollment.md](people-enrollment.md)). Only an agent holding `review:findings` can post
findings, so posting agents share the owner's root. The complete default flow needs three roots: an enrolled person
submits, an owner-enrolled reviewer agent posts, and a second enrolled person resolves (the owner's sessions get 403
`same_person`; the submitter gets 403 `same_principal`). A job submitted by an owner-enrolled agent or by the owner's session cannot receive
findings in default mode. The operator can use `tessera serve --review-single-owner` (off by default), which refuses
only the identical principal. A person's own agent path does not exist yet and is an open question in the ADR.

Everything is recorded: `separation_basis` on the decision, its event, the resolve response and reads is
`distinct_person` or `single_owner_opt_in`, or `legacy_unrecorded` for a decision made before the basis was
recorded (the schema-80 upgrade marks such rows this way, never by guessing; it counts as neither separation,
and the application never writes it for a new decision); the job always shows `findings_separation_basis` (only the two real
values); and `review_single_owner` appears in `GET /external-agent/status` and `tessera_mcp status`.

## Enrolling a person (operator opt-in)

`tessera_enroll_person`, `tessera_list_people` and `tessera_renew_person` are registered only on a separate
server started with `--person-enrollment-tool` and the owner's credential file. Enrol generates the token,
writes the person's credential file (mode 0600, created exclusively) and returns only ids, label, expiry and
the file path, never the token. See [people-enrollment.md](people-enrollment.md). These are not review agent tools.

## Code review jobs

`kind: code_review` reviews a content-addressed head snapshot, or the change between a base digest
and a head digest, against a pinned rubric ([ADR 0092](decisions/0092-code-review-jobs.md)). Submit
adds a `code_review` object: `mode` (`snapshot` or `diff`), `head_digest`, `base_digest` (required
for `diff`, absent for `snapshot`, different from the head) and `rubric_digest`. It is required for
this kind and refused for every other. Each of those digests must also be listed in `inputs`, so
derived data class, sealed-holdout refusal and policy apply to them. Head and base digests must be
snapshots already in the project's custody (`input_not_found` otherwise). `tessera_list_review_jobs` takes an
optional `kind` filter. Submitting from a git branch is described at the end of this section.

A completed code review carries numbered findings (ids `1` to `n`), a verdict (`MERGE`,
`MERGE-WITH-FOLLOWUPS` or `CHANGES-REQUIRED`) and a model-call receipt, and the job shows a review
receipt digest. A blocking finding requires `CHANGES-REQUIRED`; `MERGE` allows no major or blocking
finding. The verdict and findings are advisory and untrusted text: they never merge, approve or
unlock anything. Accept or reject findings with `tessera_resolve_review_finding` as above.

A separate runner executes these jobs (at most 2 at once by default, 1 to 8 configurable, the
minimum across runners on the host, only with enough free memory, with one automatic retry if the
model process vanishes). With no runner configured, a code review stays `queued`. The runner starts
only Tessera's built-in model child, which calls the Anthropic Messages API through the allowlisted
client with an API-key credential held by reference (`--model-profile` and
`--credential-name anthropic-api-key`; the credential directory must hold only that file), never an
environment variable. Egress must be `model_runtime` and the destination `anthropic`. The receipt
tier must equal the job tier, its input digests and class must match the job's inputs, and a billed
cost (`cost_cents`) over the budget fails the job; the job shows `attempt_receipts` and
`cost_cents_total` even for failed attempts. A billed call with unknown usage (timeout or server
error after sending) is recorded as `reserved_worst_case`, an over-estimate; it never completes a
job, is flagged not bound and is counted in the total. The runner needs an operator-written
`--policy module:attribute`;
[docs/examples/code_review_policy.py](examples/code_review_policy.py) is an example to copy, not a
default. A runner-completed job records no findings actor.

### Submitting a review from a platform git branch

`tessera_submit_code_review` (scope `review:submit`, [ADR 0108](decisions/0108-code-review-submit.md))
submits a `code_review` job for a branch on the platform's own git remote, so no digests or snapshots
are prepared by hand. The body names `repo` (an operator-mapped name), `branch`, `head_sha` (40
lowercase hex), optional `base_branch` (default `main`), `tier`, `destination_id` (default
`anthropic`), `budget_cents` and `focused_tests` (1 to 20 files). Control reads a local bare
repository only (no network git), checks that the branch is at `head_sha`, finds the merge-base with
the base branch and adopts snapshots of the changed files only (base, head and a generated rubric),
then submits a `diff` job with the pinned prompt for the round. Refusals use stable reasons
(`git_remote_not_configured`, `branch_not_found`, `head_mismatch`, `base_not_found`, `no_changes`,
`change_too_large`, `unsupported_entry`, `focused_tests_invalid`, `review_chain_mismatch`,
`review_round_limit`, `review_already_merge`, `recheck_findings_invalid`).

Focused tests only: each entry is a relative POSIX path under a `tests/` directory segment, named
`test_*.py`, a regular file at the head, with no glob characters, `..` or directory. A directory or
the whole test tree cannot be named. To re-review, pass `previous_job_id` (a completed review of the
same repo and branch) and `recheck_findings` (finding numbers of that job). The re-review checks only
those findings and reports each unresolved one with category `recheck:<n>`. At most 3 rounds; a
fourth is refused, and so is a re-review after a `MERGE` verdict. The verdict, the findings and the
source (repo, branch, head and base SHAs, focused tests, round) are written to the job's review
receipt; the job shows `code_review.source` and `code_review.review_receipt`. The result is advisory and authorizes nothing.

The same submit is available from the command line:

    tessera-review submit --repo R --branch B --head SHA --tier sonnet --budget-cents N \
        --test tests/test_x.py [--test ...] [--previous JOB --recheck N ...]

It prints the job id, round and state as JSON. The submit is atomic: the snapshots, job, source row,
events and idempotency record commit in one transaction, so a refused submit leaves nothing, and a
replay of the key returns the same job without reading git. Snapshots adopted this way are
`private_derived`, recorded with a `workspace.snapshot_adopted` event (origin `code_review_submit`);
the client supplies no bytes.
