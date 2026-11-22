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
   | `cost_basis` | `computed_from_pinned_prices` for Anthropic; `reserved_worst_case_estimate` for an Anthropic call whose usage is unknown (§5); `worst_case_for_substituted_model` for an Anthropic reply from another model (§5); `included_subscription_usage` when the OpenAI included-OAuth mode reports zero cost, otherwise `reported_cost` |
   | `outcome_class` | present only on an Anthropic call that halts the run: `model_substituted`, `output_unrecorded` or `uncertain` (§5) |
   | `cost_cents_estimate_max` | the preflight worst-case cost in cents for this call |
   | `output_sha256` | sha256 of the completion bytes |
   | `turn`, `stop_reason`, `tool_use_count` | the 1-based model turn within the run, the response's stop reason and the number of tool-use blocks it requested (Anthropic runtime; see decision 7) |
   | `status` | `settled` (a receipt), `pending` (a billed call in flight: its worst-case reservation) or `void` (a reservation released after a definite rejection; zero tokens and cost) (§5). Rows from older state directories read as `settled` |
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

   **Every billed or possibly billed call is receipted and counted.** Each billed request
   (a single-call prompt, every tool-loop turn and every resumed turn) goes through one path:
   - *Reserve before sending.* After its preflight passes and before the request is sent, the
     supervisor writes the call's `model_calls` row with `status: "pending"`, holding the worst
     case: tokens = counted input + `max_output_tokens_per_call`, cost =
     `cost_cents_estimate_max`, `cost_basis: "reserved_worst_case_estimate"`,
     `outcome_class: "uncertain"`, and the turn. The same SQLite transaction adds the
     reservation to the run's cumulative tokens and cost (which the grant-budget fence reads)
     and to the UTC-day ledger (which lives in the row), so every later preflight, of this run
     or another run in the state directory, counts it at once. If this write fails, the
     request is not sent (`model_call_unrecorded`, effect `not_sent`). The daily caps are
     enforced in that same transaction, which takes the write lock first (`BEGIN IMMEDIATE`),
     re-reads the UTC day's settled and pending totals for every run in the state directory,
     and writes nothing if the reservation would exceed `daily_token_cap` or
     `daily_cost_cap_cents`. Two supervisors sharing a state directory therefore cannot both
     pass and jointly exceed a cap. The preflight is only the early check; this one is
     authoritative. A refused reservation sends nothing and halts `daily_cap_exceeded`
     (turn 1: effect `not_sent`; a later turn: halted, not resumed). Store connections wait
     on a busy timeout for another writer's lock instead of failing.
   - *Settle on the response.* One transaction replaces the reservation in place with the
     call's charge, as described in the list below. It can be the actual usage, the worst-case
     charge of a substituted model, or the reservation itself for an unknown outcome. The run
     totals and the ledger move by the difference, and the row becomes `status: "settled"`; it
     keeps the reservation's ledger day. A failed settlement rolls back as a whole, so the
     reservation stays and the run halts fail-closed. That reservation becomes the call's
     uncertain receipt (below).
   - *Release a definite rejection.* A 429, 529, 402, `billing_error`, a 4xx request error, or
     an egress refusal before the request sent nothing billable. One transaction sets the row
     to `status: "void"` with zero tokens and cost and takes the reservation off the run totals
     and the ledger. Void rows are kept for audit but are not receipts: the default
     `model_calls` listing omits them, and the printed run receipt shows every row with its
     `status`. The supervisor drops its in-flight reservation only after the void commits. If
     the release fails, the reservation stays held and stands as an uncertain receipt, so the
     call is never journaled `not_sent` or resent, and the run halts `model_call_unrecorded`. Refusals before the reservation (a failed `count_tokens`, or a cap refusal
     during the preflight) write no row.
   - *Interrupted calls.* A row still `pending` when the supervisor next ticks the run was cut
     off by a dead process (kill, OOM, restart). It becomes a settled receipt as is
     (`reserved_worst_case_estimate`, `outcome_class: "uncertain"`), with no further change to
     the run totals or the ledger because the reservation was already counted. The run halts
     `model_call_uncertain` and the call is never resent. Like the tool-row check (decision
     7), this runs before any restore or resume, and it also runs for runs that have already
     ended.
   So every billed or possibly billed call has a durable row, counted against the run and the
   daily caps, from before it is sent; a crash at any point leaves at worst its reservation as
   its receipt. The OpenAI/OpenCode path is unchanged. A billed harness without the
   reservation hooks (test fakes only) gets its row appended after the call, in one
   transaction with the run totals.
   - A 200 is first checked for a usable message: a JSON object with a `content` list, valid
     `usage` token counts and a string `model`. Anything else (empty, unparseable, or missing
     one of these) is `model_call_uncertain` below, never a substitution.
   - `model_substituted` (a usable 200 naming another model): the other model's rates may
     exceed the pinned prices, so the call is charged the worst case: cost =
     `max(actual tokens at the pinned prices, cost_cents_estimate_max)` (or, when the operator
     pins the optional `substitution_pricing_cents_per_mtok` table, each price at least the
     pinned one and part of the policy digest only when present, the actual is priced at that
     table instead) and total tokens =
     `max(actual, counted input + max_output_tokens_per_call)`, with
     `cost_basis: "worst_case_for_substituted_model"`; the `tokens` breakdown stays the
     response's actual usage. `model_reported` is as the response states it (only if it is a
     well-formed model id), `outcome_class: "model_substituted"`. The completion is not kept.
   - `model_output_unrecorded` (a 200 whose completion could not be written): actual usage
     and cost, `outcome_class: "output_unrecorded"`.
   - `model_call_uncertain` (a timeout or transport failure after sending, or an accepted 200
     that is not a usable message): the usage is unknown, so the preflight worst case is reserved:
     tokens = counted input + `max_output_tokens_per_call`, cost = `cost_cents_estimate_max`,
     `cost_basis: "reserved_worst_case_estimate"`, `outcome_class: "uncertain"`: the settlement
     keeps the pending reservation. Every preflight already saw it while the call was in
     flight, so an unknown outcome cannot escape the daily caps. The reservation is an upper
     bound for one call, not a vendor figure, and it is not reconciled later.
   - A non-200 on the billed call that is neither a definite rejection (429, 529, 402,
     `billing_error`) nor a 4xx request error (400, 401, 403, 404, 413 and the rest of 4xx),
     for example 500–504: the outcome is unknown, so the same worst case is reserved
     (`outcome_class: "uncertain"`); the halt code stays `model_call_failed`. 4xx request
     errors are not billed: their reservation is voided, so they have no receipt. A failed
     `count_tokens` call is free and comes before any reservation, so it has no row.

   **Timeouts and streaming.** `count_tokens` is not streamed and uses a fixed 30 s timeout.
   The billed `/v1/messages` call is streamed (`"stream": true`) through the same allowlisted
   transport (https, exact allowlisted host on port 443 checked before connecting, no
   environment proxies, redirects refused, at most 16 MiB of event bytes and 1 MiB per line).
   Its server-sent events are reassembled into the same body the non-streaming call returns,
   so every check above applies unchanged. Two bounds apply: an idle timeout between bytes,
   which also bounds the connect and the wait for headers (optional
   `limits.stream_idle_timeout_seconds`, an integer 10–300, default 60), and a total cap,
   checked between reads (optional `limits.request_timeout_seconds`, an integer 30–3600, else
   `min(3600, 120 + 0.1 * max_output_tokens_per_call)` seconds: 220 s at 1000 output tokens,
   about 3397 s at 32768). The socket timeout is the smaller of the two, and a call ends
   within the total cap plus one idle timeout. Each optional limit is part of the policy
   digest only when present, so existing policy digests are unchanged. Outcomes:
   - A non-200 status (for example 429 or 529), or an `error` event before `message_start`,
     maps exactly as above. The event's error type stands for its documented status
     (`rate_limit_error` 429, `overloaded_error` 529, `billing_error` 402, request errors
     4xx, `api_error` or an unknown type 500), and its message text is dropped.
   - A stream that breaks, goes idle past the idle timeout, passes the total cap or a byte
     bound, is malformed, reports an `error` after `message_start`, or ends without
     `message_stop` has an unknown outcome. It ends `model_call_uncertain` with the reserved
     worst case above, even when `message_start` or `message_delta` already reported partial
     usage, and it is never replayed.
   Partial text is discarded and is never logged. Completion text leaves the adapter only
   through the output path described below. An injected transport without streaming support
   (test fakes only) makes the non-streaming call with the derived single read timeout
   `min(600, 60 + 0.04 * max_output_tokens_per_call)`, or `request_timeout_seconds` when set.

6. **Effects and outputs.** A request the API definitely refused (cap or egress refusal, 429,
   529, quota) marks the prompt effect `not_sent` and voids that call's pending reservation
   (§5); a rate or quota condition is retried with the
   same prompt and model after its backoff, at most 8 attempts, then the run halts
   `model_retry_exhausted`. A failure whose outcome is unknown stays `uncertain` and is never
   replayed. A call in flight when the process dies leaves its effect begun and its row
   `pending`. The next tick settles that row as the uncertain worst-case receipt (§5) and halts
   the run `model_call_uncertain` without resending. The completion is written only to a
   private file `<state_dir>/outputs/<run_id>-<seq>.txt` (directory 0700, file 0600), where
   `seq` is that call's settled row. The digest is added to the settled receipt as
   `output_sha256`; a completion that cannot be written marks the same row
   `outcome_class: "output_unrecorded"` instead. The supervisor receives the manager credential directory path
   (`CREDENTIALS_DIRECTORY`) so it can read the key file at call time; the key itself is never in
   any environment or argument.

7. **Tool use in the Anthropic runtime (owner direction, 2026-11-22).** The direct Messages API
   runtime runs a multi-turn loop over Tessera's own MCP tools; no other tool source exists.
   - *Deny by default.* The profile `permissions` are the deny-all rule followed by zero or
     more exact `{"action":"tessera_<name>","resource":"*","effect":"allow"}` rules. Zero allows
     means no tools are offered (a single user message, as before). Only allowed tools are
     offered to the model or executed; a call to any other name is answered with a tool error
     without contacting the server. Any other rule shape is refused at admission.
   - *Same server, same token.* Tools run through the existing `tessera_mcp` server, which reads
     the agent token from `<CREDENTIALS_DIRECTORY>/agent-token` by path (never in argv, the
     environment or logs). The control API re-checks scopes on every call; the profile allowlist
     narrows, never widens, what the token can do.
   - *Catalog pin.* When any tool is allowed the profile carries `tool_catalog_sha256`, the
     sha256 of canonical JSON of the offered tool list (sorted by name, each `{name, description,
     input_schema}`), as printed by `tessera-reasoning catalog` (no model call). A mismatch, or an allowed tool the server
     does not offer, blocks the run with `tool_catalog_changed` before any model call.
   - *Bounds.* `max_steps` (1..16) bounds model turns per run; `limits.max_tool_calls` (1..64)
     bounds tool calls per run (exceeding it halts `tool_call_cap_exceeded`);
     `limits.max_tool_result_bytes` (1..65536) truncates each result before it reaches the model.
     A run still requesting tools after its last turn ends `failed` with `turn_limit_reached`;
     receipts are kept. The tools requested on that last turn are not executed but are still
     receipted as `refused` with `skip_reason` `turn_limit_reached` (see the status model
     below); that turn's model receipt carries `unrun_tool_count`.
   - *One model-call row per turn.* Each turn appends a `model_calls` row (decision 1) with
     `turn`, `stop_reason` and `tool_use_count`. Per-call and daily token and cost caps and the
     grant budget are counted across turns and checked (token count preflight, decision 3)
     before each billed turn, so a long loop cannot exceed them.
   - *Receipt and count rule per turn.* Decision 5's rule applies to every turn, not only the
     first: a turn whose reply names another model (`model_substituted`) is that turn's row
     charged the substituted-model worst case of §5; a turn whose completion could not be
     written (`output_unrecorded`) is that turn's row with its actual usage; a turn
     whose outcome is unknown (timeout or transport failure after sending, an accepted 200
     without usable usage, or a 5xx on the billed call) is that turn's row reserving that
     turn's preflight worst case (its counted input + `max_output_tokens_per_call`,
     `cost_basis: "reserved_worst_case_estimate"`, `outcome_class: "uncertain"`). Each such row
     carries `turn` (and `stop_reason` when a reply arrived). It is the turn's own reservation
     row (§5), reserved before the turn was sent and settled exactly once, and it halts the
     run; the turn is never replayed. Every turn, including a resumed one, is reserved before
     sending, so a crash during any turn's request leaves that turn's reservation as its
     uncertain receipt. A definite rejection of a later turn voids its reservation and still
     parks the loop for resume (below), so it has no receipt. Every turn uses the
     per-request timeouts of decision 5: 30 s for `count_tokens`, and
     `limits.request_timeout_seconds` or the derived default for the billed call.
   - *Per-tool-call digest receipts.* An append-only `tool_calls` table (created on open, so
     older state directories upgrade in place) holds one row per tool call: `run_id`, `seq`,
     `turn`, `index`, `name`, `input_sha256`, `result_sha256`, `is_error`, `truncated`, `allowed`,
     `status`, `skip_reason`, `observed_at`, `completed_at`. Tool inputs and results are never
     stored or journalled; they exist only in the conversation held by the adapter (and in the
     private pending file below while a loop is paused). The printed receipt carries
     `tool_calls`.
   - *Tool-row status model.* Every `tool_use` block the model requests gets exactly one row
     whose `status` is one of `started`, `succeeded`, `error`, `uncertain`, `refused`, `denied`.
     There is no separate `executed` flag: a call was executed (or may have been) iff its
     status is `succeeded`, `error` or `uncertain`.
     - *Intent before action.* An allowed call follows the `begin`/`finish` effect pattern: an
       intent row with status `started` is written before the call and completed exactly once
       to `succeeded`, `error` or `uncertain`. A call whose outcome is unknown (timeout,
       transport loss, or a crash before completion) is `uncertain`: rows still `started` when
       the supervisor next ticks the run are marked `uncertain`, the call is never retried, and
       the run halts `tool_call_uncertain`. This check runs before any restore or resume of a
       parked loop, so a parked run with a `started` row halts without replay and its pending
       file is deleted.
     - *Never executed.* A call to an unlisted or disallowed tool is `denied` (answered with an
       error result, so it has `result_sha256`, `is_error` true and `truncated`). A call that
       is not run because a fence tripped before it, or because it was requested on the last
       allowed turn (no turn is left to return its result), is `refused` with `skip_reason`
       naming the fence code or `turn_limit_reached`; that turn's model receipt also carries
       `unrun_tool_count`. `refused`, `started` and `uncertain` rows have null `result_sha256`,
       `is_error` and `truncated`; only `refused` rows have a `skip_reason`.
     - *Migration.* Older state directories are rebuilt in place from either legacy shape: rows
       without status become `denied` (not allowed), `error` or `succeeded`; rows of the
       interim `executed`/`skip_reason` shape with a skip reason become `refused` with that
       reason and null result fields.
   - *Per-turn aggregated tool provenance.* Each turn that requested tools enqueues one
     provenance record once every row of that turn is terminal (none `started`): kind `tool`,
     outcome `unknown` if any row is `uncertain`, else `failed` if any is `error`, else
     `succeeded`; `tool_id` `tessera_tool_turn`, transition `tool_turn.<turn>`, and
     `content_digest` (`sha256:<hex>`, the provenance envelope's digest form) of the canonical
     JSON list of that turn's tool receipts (each `{index, name, input_sha256, result_sha256,
     is_error, truncated, allowed, status}`). The record carries an optional `tool_turn: {turn,
     calls, errors, denied, truncated, unrun, refused, uncertain}` (turn 1..16, the counts
     0..64): `calls` is every row of the turn, `errors`, `denied` and `uncertain` count those
     statuses, `unrun` counts `refused` rows with `turn_limit_reached`, `refused` counts the
     other (fence) refusals and `truncated` truncated results. `tool_turn` is omitted from the
     wire when absent, and `refused`/`uncertain` are optional (omitted when absent) so records
     written before them stay valid and existing digests are unchanged. A turn cut off by a
     crash gets its record when the next tick marks its `started` rows `uncertain` (in the same
     transaction); if the run is never ticked again, or that write is refused, the turn stays
     journal-only (its `tool_calls` rows remain). The per-run outbox stays at most 64 records
     and 8 slots are reserved for terminal and state transitions: a tool-turn record is written
     only while the run's outbox holds fewer than 56 records. The first turn that would exceed
     that writes one record with `tool_id` `tessera_tool_turn_overflow` (outcome `unknown`,
     `tool_turn` of the overflowing turn); later tool turns are journal-only (the `tool_calls`
     rows remain complete). With `max_steps` at most 16 this is not expected to trigger.
   - *Kill switch before every turn and every tool call.* Before every turn after the first, and
     before EACH allowed tool call, the supervisor re-runs its fence: stop request, grant
     revocation or expiry, run duration, grant budget and the daily cap. A tripped fence halts
     the run without another model call; if it trips before a tool call, that call and the
     remaining calls the turn requested are recorded `refused` with the fence code, none runs,
     and the run halts with that code.
   - *Backoff without replay.* A definite rejection (rate limit, quota, cap or egress refusal)
     on turn 1 behaves as in decision 5. After turn 1 the rejection is marked as resumable, and
     after the backoff the supervisor resumes at the rejected turn (re-running the token count
     and caps) with the same model; no completed turn and no tool call is replayed, and the
     prompt effect stays acknowledged. The 8-attempt cap applies. An uncertain outcome
     (timeout, transport failure, a tool call whose result is unknown: `tool_call_uncertain`)
     drops the pending conversation and is never replayed; an uncertain model turn is
     receipted and counted as above and leaves no park.
   - *Resume records.* Each resume of a parked turn writes a `loop_resumes` row and one
     provenance record (kind `agent`, outcome `started`, `content_digest` `sha256:<state digest>`)
     with transition `resume.<turn>`; a later resume of the same turn uses `resume.<turn>.<k>` so
     no record is silently deduplicated.
   - *Durable pause.* When a loop parks, the adapter hands the supervisor a version 1 snapshot
     (run id, the turn to retry, the conversation exactly as sent, the offered-tools digest,
     tool-call and turn counts, the configured model and the cumulative tokens, cost and usage;
     never the API key or headers). The supervisor writes it to the private file
     `<state_dir>/pending/<run_id>.json` (directory 0700, file 0600, created `O_EXCL` and
     `O_NOFOLLOW`, replaced atomically via a temporary file and rename) and records a
     `loop_parks` journal row (`run_id`, `seq`, `turn`, `state_sha256`, `observed_at`): the digest
     only, never the content. If the snapshot cannot be recorded the loop is dropped and the
     outcome is uncertain (`model_park_unrecorded`). When the backoff elapses, in the same
     process or after a restart, the supervisor reads the file, verifies its sha256 against the
     latest `loop_parks` row, and the adapter validates strictly (version, run id, configured
     model, current tool-catalog digest, counts within limits, well-formed alternating messages)
     and that the snapshot's cumulative values equal the run totals in the store and its
     `tool_calls_used` equals the journal's answered tool rows (`succeeded` and `error`, which
     were executed, plus `denied`, which were answered with an error result; all count against
     `max_tool_calls`). A parked run has no other rows; a `started` row halts the run before
     any restore (above). Only then does
     it resume at the parked turn, and it records a `loop_resumes` row (`run_id`, `seq`, `turn`,
     `state_sha256`, `restored_from_disk`, `observed_at`) as the resume receipt plus one
     provenance record (kind `agent`, outcome `started`, `content_digest` the state digest,
     transition `resume.N`). A missing file, digest mismatch or any invalid state halts the run
     `model_resume_state_invalid` without a model call. Caps already consumed survive because
     the run totals and the daily ledger live in the store. The file is deleted when the loop
     completes, halts or is stopped. The printed receipt carries `loop_parks` and
     `loop_resumes`. The conversation (prompt and tool results) is therefore at rest in that
     private file while a loop is paused, protected by file permissions only; only its digest is
     journalled.
   - *Egress unchanged.* The model host stays only on the model-runtime allowlist and Tessera's
     transport; tool calls go to the control plane through the existing MCP server. No host is
     added to any allowlist.
   - *Gap.* The OpenAI/OpenCode path already runs tools inside its pinned harness, with its own
     permission rules; it does not get per-tool-call digest receipts, and no per-turn model
     rows beyond what the harness reports.

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
  new `tool_turn` provenance field (including its `refused` and `uncertain` counts) and the
  run halts at its outbox bound.
- Callers of `model_calls(run_id)` now receive a list of rows rather than one cumulative row.
- The preflight is an estimate: the actual output may be smaller than the cap, so the worst-case
  reservation can refuse a call that would have fit. That is the intended direction of error.
- Receipt field names (`model_configured`, `model_reported`, `model_identity_basis`, `tier`,
  `input_class`, `prompt_sha256`, `profile_sha256`, `input_sha256`, `input_digests`,
  `policy_sha256`, `limits`, `tokens`, `daily_tokens_after`, `daily_cost_cents_after`, `cost`,
  `cost_basis`, `cost_cents_estimate_max`, `output_sha256`, `outcome_class`, `turn`,
  `stop_reason`, `tool_use_count`, `unrun_tool_count`, `observed_at`) are the contract
  later ADRs attach verbatim.
- Tool inputs and results are visible to the model and the control plane but are not
  receipted beyond their digests; a reviewer can match a digest only if they hold the content.
- No live model call was made to build or test this record. The first receipted run is an
  operator step under ADR 0082.

## Notes

Direction is recorded as owner direction, 2026-11-22. No private messages are quoted.
