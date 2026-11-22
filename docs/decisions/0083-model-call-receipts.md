# 0083 — One receipt per model call; caps enforced before the call; limits back off, models never downshift

## Status

Proposed (2026-11-22, owner direction). Implements slice 3 of the on-platform agent-loop work.
Builds on [ADR 0082](0082-agent-loop-anthropic-destination.md) (enrolled destinations and
policy tiers). Extends the existing private reasoning journal and the research-journey receipt;
no second receipt schema is added. Numbers 0069-0081 are claimed by other in-flight records;
the integrator may renumber this record at merge. ADR 0084 and ADR 0085 are planned and not
yet on main.

## Context

The reasoning supervisor already fenced usage (`max_steps`, `max_tokens`,
`max_duration_seconds`, a grant budget, and a fence that halts on any non-zero cost for
non-billed destinations) but recorded only a running token total and cost per run. Nothing said
which model ran, under which policy, against which inputs, or why a run paused. Owner direction
(2026-11-22) is that model usage from Tessera's own loop is receipted per call, that per-call
and daily token and cost caps hold before money is spent, that rate limits and quota are
surfaced with a backoff, and that the loop never silently moves to a cheaper model.

## Decision

1. **One append-only receipt row per model call.** The reasoning journal's `model_calls` table
   (created on open, so older state directories upgrade in place) holds one row per call, keyed
   by `(run_id, seq)`. Rows are appended and never updated or deleted. Fields:

   | field | meaning |
   |---|---|
   | `run_id`, `seq` | run and 1-based call sequence within the run |
   | `model_configured` | qualified `provider/id` from the admitted profile |
   | `model_reported` | `provider/id` the response reports, or null |
   | `model_identity_basis` | `harness_reported_unverified` for the OpenAI harness path; the Anthropic path records the response's own `model` field and never claims it is verified |
   | `input_class` | the profile's declared input class: `public`, `private_code_approved` or `private_derived`; missing reads as `private_derived` ([ADR 0039](0039-data-class-guard.md)); declared by the operator, not checked against the prompt |
   | `tier` | policy tier from ADR 0082 (`sonnet`, `opus`, `haiku`, `fable`, or `unclassified` for OpenAI) |
   | `prompt_sha256`, `profile_sha256` | digests bound at run start |
   | `input_sha256` | digest of the admitted prompt bytes |
   | `input_digests` | optional name → sha256 map the profile declares (for example private snapshots); a malformed digest blocks the run |
   | `policy_sha256` | sha256 of canonical JSON of the admitted policy (account mode, provider, model, tier, usage bounds, permissions and, for Anthropic, the pinned prices); credential references are never part of it |
   | `limits` | the per-call and daily caps in force for this call |
   | `tokens` | `{input, output, reasoning, cache_read, cache_write, total}` as reported, or `{total}` when no breakdown exists |
   | `daily_tokens_after`, `daily_cost_cents_after` | the UTC day's totals across all runs in the state directory, including this call |
   | `cost` | for Anthropic, cents computed from the operator-pinned prices; for OpenAI the harness-reported number, no currency claimed |
   | `cost_basis` | `computed_from_pinned_prices` for Anthropic; `reserved_worst_case_estimate` for an Anthropic call whose usage is unknown (§5); `included_subscription_usage` when the OpenAI included-OAuth mode reports zero cost, otherwise `reported_cost` |
   | `outcome_class` | present only on an Anthropic call that halts the run: `model_substituted`, `output_unrecorded` or `uncertain` (§5) |
   | `cost_cents_estimate_max` | the preflight worst-case cost in cents for this call |
   | `output_sha256` | sha256 of the completion bytes |
   | `observed_at` | epoch seconds |

   The completion text is stored only in a private output file under the run's state
   directory, created private (0600); it is never a journal or receipt field. Prompts, completions,
   upstream error text, request ids and credential values are never journal or receipt fields;
   the store refuses any key outside the allowlisted field set.

2. **Receipts carry the records.** `tessera-reasoning run|status` prints `model_calls` (a list
   of rows) with its existing receipt. A research journey's `receipt()` carries
   `model_calls: {"planner": [...], "critic": [...]}` from each role's journal. A campaign
   receipt that later attaches a reasoning run or journey carries the same list verbatim as
   metadata; it never treats `model_reported` as verified. Wiring a reasoning run onto a
   campaign receipt is part of ADR 0084, which owns the research and review job record.

3. **Caps are enforced before the billed call.** The profile carries `limits`
   (`max_output_tokens_per_call`, `max_input_tokens_per_call`, `max_cost_cents_per_call`,
   `daily_token_cap`, `daily_cost_cap_cents`) and operator-pinned
   `pricing_cents_per_mtok` (`input`, `output`, `cache_read`, `cache_write`); both are part of
   the policy digest. Before each billed request the client counts input tokens with the
   Messages API `count_tokens` endpoint on the same allowlisted host, then refuses (code
   `model_call_cap_exceeded`, nothing sent to `/v1/messages`) unless the input tokens are within
   `max_input_tokens_per_call`, the worst-case cost (input tokens at the input price plus the
   per-call output cap at the output price) is within `max_cost_cents_per_call`, within what
   remains of the UTC-day token and cost caps, and within the grant's remaining budget
   (Tessera's own spend limit; the supervisor's `budget_check`). The output cap is sent as
   `max_tokens`. After the call the daily totals are re-checked with the actual usage and the
   run halts `daily_cap_exceeded` if a cap was exceeded after the fact. Cost is computed from
   the reported usage and the pinned prices; it is an estimate of billing, not a vendor invoice,
   and the operator must keep the pinned prices current. For non-billed destinations (OpenAI
   included OAuth) a non-zero reported cost still halts the run (`usage_fence`); billed
   destinations are fenced by run cost against the grant's remaining budget and the caps above.

4. **Rate limits and quota are recorded and surfaced.** HTTP 429 or error type
   `rate_limit_error`, and HTTP 529 or `overloaded_error`, map to `model_rate_limited`; error
   type `billing_error` (or HTTP 402) maps to `model_quota_exhausted`. The OpenAI harness path
   maps its 429, 402 and session error classes to the same conditions. Only the status code, the
   error type and a numeric retry hint (`retry-after`) are read, never a body or message. Each
   condition is a `model_rate_limits` journal row and is printed as
   `rate_limit: {condition, backoff_until}`. Control-plane endpoints keep their existing generic
   failure.

5. **Back off, never downshift.** These conditions are definite rejections: the request was
   not accepted, so the prompt effect is marked not sent and is retried only after the
   backoff, with the same model. During a backoff the supervisor still runs every fence (stop,
   expiry, revocation, budget, caps) and then returns without calling the model. The backoff
   uses the retry hint clamped to 1 s–24 h, or 60 s for a rate limit and one hour for quota. The
   configured model, tier and binding never change because of a limit; a cheaper model is chosen
   only by a new, separately admitted profile. The response `model` must equal the configured
   id, or the configured id plus a dated suffix (`-` and eight digits); anything else halts the
   run with `model_substituted`. Any other failed response is `model_call_failed`. A transport
   failure or timeout leaves the outcome unknown (`model_call_uncertain`): such a call is
   never replayed and the run ends `uncertain_prompt`. The run stays bounded by its duration
   fence.

   **Every billed or possibly billed call is receipted and counted.** Before the run halts, the
   supervisor appends a `model_calls` row for the call, adds it to the UTC-day ledger and to the
   run cost (which the grant-budget fence reads):
   - `model_substituted` (a 200 naming another model): the response's actual usage and cost
     computed from the pinned prices, `model_reported` as the response states it (only if it
     is a well-formed model id), `outcome_class: "model_substituted"`. The completion is not
     kept.
   - `model_output_unrecorded` (a 200 whose completion could not be written): actual usage
     and cost, `outcome_class: "output_unrecorded"`.
   - `model_call_uncertain` (a timeout or transport failure after sending, or an accepted 200
     without usable usage): the usage is unknown, so the preflight worst case is reserved:
     tokens = counted input + `max_output_tokens_per_call`, cost = `cost_cents_estimate_max`,
     `cost_basis: "reserved_worst_case_estimate"`, `outcome_class: "uncertain"`. A later run's
     preflight the same UTC day sees the reservation, so an unknown outcome cannot escape the
     daily caps. The reservation is an upper bound for one call, not a vendor figure, and it is
     not reconciled later.
   - A non-200 on the billed call that is neither a definite rejection (429, 529, 402,
     `billing_error`) nor a 4xx request error (400, 401, 403, 404, 413 and the rest of 4xx),
     for example 500–504: the outcome is unknown, so the same worst case is reserved
     (`outcome_class: "uncertain"`); the halt code stays `model_call_failed`. 4xx request
     errors are not billed and carry no row. A failed `count_tokens` call is free and carries
     no row.

   **Timeouts.** `count_tokens` uses a fixed 30 s timeout. The billed `/v1/messages` call is
   not streamed, so nothing arrives until generation ends; its read timeout is derived from the
   output cap: `min(600, 60 + 0.04 * max_output_tokens_per_call)` seconds (100 s at 1000 output
   tokens, 600 s from 13500). The operator may instead set the optional
   `limits.request_timeout_seconds` (an integer, 30–600); when present it is used for the
   billed call and is part of the policy digest, and when absent the derived default applies
   and existing policy digests are unchanged. A call that takes longer than the timeout in
   force ends `model_call_uncertain`
   with the reserved estimate above and is never replayed. Streaming the response, which
   would remove this limit, is follow-up work.

6. **Effects and outputs.** A request the API definitely refused (cap or egress refusal, 429,
   529, quota) marks the prompt effect `not_sent`; a rate or quota condition is retried with the
   same prompt and model after its backoff, at most 8 attempts, then the run halts
   `model_retry_exhausted`. A failure whose outcome is unknown stays `uncertain` and is never
   replayed. The completion is written only to a private file
   `<state_dir>/outputs/<run_id>-<seq>.txt` (directory 0700, file 0600); the receipt carries its
   `output_sha256`. The supervisor receives the manager credential directory path
   (`CREDENTIALS_DIRECTORY`) so it can read the key file at call time; the key itself is never in
   any environment or argument.

## Consequences

- A reviewer can tell which model produced a reasoning output (by hash), under which policy and
  limits, against which input digests, what each call cost and the day's running totals, without
  seeing the prompt or the completion.
- `model_reported` is the response's or harness's statement; an independent witness is future
  work.
- Each rate-limit event also enqueues one provenance record (kind `model`, outcome `failed`,
  `error_code` `model_rate_limited` or `model_quota_exhausted`, and
  `rate_limit: {backoff_until, tier?, policy_sha256?}`) in the same journal transaction. The
  control-side provenance model accepts exactly these codes and keys. When a run's outbox bound
  is reached the event rolls back and the run halts, rather than continuing unrecorded.
- Deploy order: upgrade the control plane before the reasoning supervisor. The provenance
  model forbids unknown fields, so an older control plane rejects `rate_limit` records and the
  run halts at its outbox bound.
- Callers of `model_calls(run_id)` now receive a list of rows rather than one cumulative row.
- The preflight is an estimate: the actual output may be smaller than the cap, so the worst-case
  reservation can refuse a call that would have fit. That is the intended direction of error.
- Receipt field names (`model_configured`, `model_reported`, `model_identity_basis`, `tier`,
  `input_class`, `prompt_sha256`, `profile_sha256`, `input_sha256`, `input_digests`,
  `policy_sha256`, `limits`, `tokens`, `daily_tokens_after`, `daily_cost_cents_after`, `cost`,
  `cost_basis`, `cost_cents_estimate_max`, `output_sha256`, `observed_at`) are the contract
  later ADRs attach verbatim.
- No live model call was made to build or test this record. The first receipted run is an
  operator step under ADR 0082.

## Notes

Direction is recorded as owner direction, 2026-11-22. No private messages are quoted.
