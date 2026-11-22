# ADR 0092: code review jobs

Status: proposed (schema v75). Extends the review jobs of
[ADR 0084](0084-research-review-journeys.md). Uses the registered model destination of
[ADR 0082](0082-agent-loop-anthropic-destination.md) and the receipt fields of
[ADR 0083](0083-model-call-receipts.md). Keeps the invariant of
[ADR 0047](0047-agent-routed-data-destinations.md) and the sealed-input refusal of
[ADR 0068](0068-sealed-holdout-evaluation-stages.md).

## Context

A code review of a branch is today run outside Tessera: a coding-agent process on a runner machine
reads a snapshot and writes notes elsewhere. Tessera records no receipt for it. It does not know
which model ran, on which content, under which policy or at what cost, and a finding has no recorded
human decision. Owner direction, 2026-11-22, was that research and review run as Tessera agent runs
with receipts. ADR 0084 gave a client a way to request a review job and to resolve its findings, but
it left the runner out and had no kind for code. This ADR adds the kind and a runner that executes it
honestly.

## Decision

### 1. A generic `code_review` kind

`code_review` is a new closed kind on the ADR 0084 contract. It names no repository, host or
experiment. A job carries a `code_review` subject:

- `mode`: `snapshot` (review one content-addressed head snapshot) or `diff` (review the change from a
  base digest to a head digest). A `diff` needs a base digest that differs from the head; a
  `snapshot` has none.
- `head_digest`, optional `base_digest` and `rubric_digest`, the pinned review rubric. Rubric text is
  never stored by control, as with the prompt digest.

Inputs are `{snapshot_id, digest}` as in ADR 0084, and control derives each input's data class from
the stored record. The head and base digests come from the existing artifact custody: they must be
content-addressed snapshot artifacts already in the project (owner decision, 2026-11-22: there is no
new snapshot step). A digest with no such snapshot is refused `input_not_found`.

The subject is required for this kind and refused (422) for every other kind. Each digest it names
must also appear as the `digest` of an entry in `inputs`. That binds the subject digests to the
inputs, so their derived data class, the sealed-holdout refusal and the review policy apply to them exactly
as they do for any input. Submit, refusals, the policy hook, approvals and states are otherwise as in
ADR 0084.

### 2. Where it runs

A job runs on a registered model destination (ADR 0082). The only supported destination is
`anthropic`, the Anthropic Messages API with an API-key credential held by reference (owner
decision, 2026-11-22). Egress must be `model_runtime`; the runner refuses a job with any other egress
(`egress_not_permitted`) or destination (`destination_not_supported`) before it starts.

The runner CLI starts only Tessera's built-in model child, `tessera_agent.code_review_child`, as
`python -I -m` with an environment of `CREDENTIALS_DIRECTORY`, `LANG` and `TMPDIR` only. There is no
operator-supplied model command. The child calls the Messages API only through the allowlisted
client of ADR 0082: the model-runtime host only, proxy settings ignored, redirects refused. So
private content reaches no other public host (ADR 0047), and no subscription login is involved.

- The runner requires `--model-profile` (a JSON ADR 0082 profile, validated at startup,
  `model_profile_invalid`) and `--credential-name anthropic-api-key`. The credential directory must
  hold only that credential (`credential_directory_not_exclusive`), so the child can read nothing
  else. The runner checks the file is a private regular file and never opens it.
- The key is never an environment variable: `ANTHROPIC_API_KEY` and similar variables are refused
  (the runner will not start with one set) and are never forwarded to the child.
- The review prompt is pinned in code. A job whose prompt digest differs from the built-in prompt's
  digest is not started (`prompt_not_pinned`).
- The child gets the content from custody: the runner loads each input's files, verifies the snapshot
  digest and passes the bytes in the child spec. A failure to load fails the job `input_unavailable`.
  The child refuses oversized input before any call (`input_too_large`).
- Budget preflight: the client computes the worst-case cost of a call from the pinned prices and
  refuses a call that would exceed the job budget before it is billed (`budget_exceeded`).

This route is billed. Its receipt uses `cost_basis: "computed_from_pinned_prices"` with an integer
`cost_cents` that may not understate it. The budget is enforced on `cost_cents`: above the job's
budget the output is refused and the job is failed (`budget_exceeded`). The receipt's
`model_identity_basis` is `provider_response_model`; it does not claim a verified model.

### 3. Model tier

The receipt tier must equal the job tier. A silent downshift is refused. A `fable` job needs the
per-job human approval of ADR 0084 before it is queued.

### 4. Output and receipts

The reviewer returns numbered findings and a verdict. Finding ids are exactly `1` to `n` in order
(`n` may be 0). Severities are those of ADR 0084. The verdict is `MERGE`, `MERGE-WITH-FOLLOWUPS` or
`CHANGES-REQUIRED`, with two consistency rules: any `blocking` finding requires `CHANGES-REQUIRED`,
and `MERGE` allows no `major` or `blocking` finding. A violation is 422 `failed_output_invalid` and
nothing is stored.

A code review never completes without a model-call receipt. It carries the ADR 0083 fields (the
configured model, the model the response reported, never claimed verified, the tier, input digests
and class, tokens, cost and its basis, the policy and output digests, the caps in force, the attempt
count) and nothing else: no prompt, completion, error text or credential. Control then stores a
review receipt that binds the job digest, mode, base, head and rubric digests, verdict, a digest of
the findings and the model-call receipt, and exposes its digest on the job. The model-call receipt
digest is the canonical digest of the validated receipt with every field present (an absent optional
field is `null`); a supplied `receipt_digest` that does not match it is refused. The completion event
carries the verdict and that digest.

Receipt binding. A receipt that completes a job, over HTTP or from the runner, is refused 422
`failed_output_invalid` unless:

- its input digests are exactly the job's inputs, keyed by snapshot id (`{snapshot_id: digest}`);
- its `prompt_sha256` equals the job's prompt digest;
- `input_class` is present and equals the most private class among the job's inputs
  (`public` < `private_code_approved` < `private_derived`);
- the provider prefix of `model_configured` equals the job's destination;
- for `anthropic`, `cost_basis` is `computed_from_pinned_prices` and `model_identity_basis` is
  `provider_response_model`, so subscription-usage and unverified-harness receipts are refused.

Every attempt leaves a receipt when one exists. The child writes the receipt to a private file right
after the billed call, before parsing the output. Completed, failed (including `budget_exceeded` and
`failed_output_invalid`) and vanished attempts append an entry to `attempt_receipts` (attempt,
outcome, reason, receipt, receipt digest, whether it passed binding). A structurally invalid receipt
is flagged `receipt_invalid` and not stored. The job exposes `attempt_receipts` and
`cost_cents_total`, the sum of the stored receipts' cost, so an over-budget call still shows its
cost. Failure and retry events carry the receipt digest when there is one.

The verdict is advisory. The job stays `advisory: true` and `execution_authorized: false`. A verdict
never merges, approves or unlocks anything. A person accepts or rejects findings with the ADR 0084
resolve tool; there are no parallel MCP tools, only a `kind` filter on the list tool and the subject
on submit.

### 5. The runner

A separate runner process claims queued `code_review` jobs and launches the built-in model child per
job. It never invents a receipt, finding or verdict.

- Bound: at most N jobs run at once across the whole host, held durably in the store (default 2,
  configurable 1 to 8). N is the minimum `max_concurrent` among the runners seen within a lease
  period, so a second runner cannot raise another's setting, and a restart cannot exceed it.
- Admission: a job starts only when free memory is at or above a configured minimum. If free memory
  cannot be read, nothing is admitted (fail closed).
- Leases: a running job holds a renewable lease. A job whose lease expires is treated as a vanished
  child. Completion and every other runner write (renew, fail, input load) require an unexpired
  lease; an expired one is a conflict and the reaper handles the job. A receipt that the job's
  lease holder sends after its lease expired (even after the reaper requeued or failed the job) is
  still recorded as a `late` attempt receipt and counted in `cost_cents_total`, without changing the
  job's state, findings or verdict; nobody else's late receipt is stored. The runner names the
  attempt it ran on every report, and a late receipt is filed under exactly that attempt, only if
  the store recorded this runner as its holder (runner ids are fresh per process). A late receipt
  is kept even when it fails binding (it is flagged `bound: false`): it records spend and can never
  complete a job. The runner keeps renewing
  through transient errors, and the child timeout must be shorter than the lease.
- Retry: when the model child vanishes (killed by a signal, timed out or exited without a parseable
  result), the job is queued once more. A second vanish marks it `failed` with `model_child_vanished`.
  A child that reports a failure is `failed` with its reason (`budget_exceeded`, `input_too_large`,
  `failed_output_invalid`, `prompt_not_pinned`, otherwise `model_failed`) and is not retried.
- Policy: the policy is checked again at launch. If it now refuses, the job becomes
  `not_started` with the policy reason.
- Truthful default: with no runner or model configured, jobs stay `queued`. The out-of-process
  runner (`python -m tessera_control.code_review_runner`) refuses to start without an operator
  policy (`--policy module:attribute`), so it never claims jobs only to mark them `not_started`.
- Operator policy: the first live code review uses an operator-written policy that admits
  `anthropic` at `sonnet` or `opus` for `private_derived` (and `public`) inputs.
  [docs/examples/code_review_policy.py](../examples/code_review_policy.py) is a generic example the
  operator copies and owns; it is not a platform default.
- Only the runner's leased claim starts a code review. The ADR 0084 `mark_running` seam refuses this
  kind, so no running job can hold a slot without a lease that can be reaped.
- Child environment: see section 2; the child gets no home directory and no API-key variable and
  reaches its credential only by reference under `CREDENTIALS_DIRECTORY`.

A runner-completed job records no `findings_actor_id` (the system runner is not a principal), and
resolve still refuses the submitter and any principal that posted findings over HTTP, so neither may
resolve the review. A code review is still never started through `mark_running`.

Every claim, retry, completion and failure writes its state change and its event in one
transaction, with a fixed system identity as the actor, and tenant checks apply at data access.

## Schema v75

The `review_jobs.kind` CHECK is widened by a table rebuild (copy, drop, rename, recreate indexes and
the no-delete trigger). A new `code_review_jobs` side table holds the subject digests, the verdict,
the attempt count, the lease owner and expiry, the receipt and review-receipt records and digest,
and `attempt_receipts_json` (a JSON list, default `[]`). A new `code_review_runners` table
(`runner_id`, `max_running` 1 to 8, `seen_at`) holds each runner's bound for the host-wide minimum.
The `code_review_jobs` row is written in the submit transaction, and it cannot be deleted.

## Consequences

- A branch review is a receipted Tessera job. A reviewer can be audited for model, tier, inputs,
  policy and cost, and a person decides on each finding.
- Until an operator wires a runner, a model profile, a credential and a destination policy, submits are refused or
  stay `queued`.
- No live or paid model call was made to build or test this change. The first receipted run is an
  operator step.

## Tests

Focused tests, all without network or paid calls (fake transports), cover: submit validation (subject
required only for this kind, base rules, digests bound to inputs, sealed and policy refusals); the
`kind` list filter; findings numbering, verdict consistency, tier equality, budget refusal and
receipt digest checks, with nothing stored on refusal; each receipt binding rule failing without its
check; review receipt binding; the advisory flags; the v75 migration and its triggers; the built-in
child (pinned prompt, destination and egress refusal, oversized input, budget preflight, receipt file
written before parsing, invalid output); runner startup refusals (profile, credential name and
directory, API-key variables, missing policy); `prompt_not_pinned`, `egress_not_permitted` and
`destination_not_supported` giving `not_started`; custody input loading; attempt receipts on
completed, failed and vanished attempts and `cost_cents_total`; the minimum host-wide bound across
runners, memory admission failing closed, lease reaping and expired-lease refusal, the single retry
then `model_child_vanished`, policy re-check; and the MCP submit and list changes.

## Open questions

1. How the runner is wired as a managed unit (service, restart policy, credential reference).
2. Whether an unlock requirement (as in ADR 0049/0051) may require a `completed` review on the
   current head digest with no open `blocking` finding.
3. Deriving the runner's policy from the registry rather than naming it with `--policy`.
