# 0093 — The agent loop submits owner-approved campaigns and reruns

## Status

Proposed. Owner direction, 2026-11-22. Builds on
[ADR 0042](0042-standing-approvals-and-scheduled-campaign-starts.md) (a human binds a spec digest to
caps), [ADR 0045](0045-campaign-gates-program-budget.md) (program cap),
[ADR 0055](0055-mcp-steering-tools.md) (scoped MCP tools) and ADR 0085 (the standing loop and its kill
switch). The number was chosen after checking every ref for the highest ADR in use (0090); the
integrator renumbers if another branch took it. Off by default.

## Context

A person or an outside session submits owner-approved campaign specs, and reruns failed ones, by hand.
That is the scaffolding the platform should absorb: the owner has already decided what may run and
what it may cost, and the repeated step is only "put that campaign in the queue".

Submitting is not the hard part. An enrolled agent already holds `campaign:submit`, and a submitted
campaign is inert until a human approves its exact digest. The approval is the authority, and an agent
must never create it, widen it or consume one it was not given (DESIGN §5.3, ADR 0042, ADR 0055). So the
design question is how to let the loop act on an approval the owner has already made, without an agent
approving anything.

## Decision

1. **An approved submission is a human record.** A person records an approved submission: either a
   spec (kind `submit`, with the digest they reviewed) or a failed campaign to rerun (kind `rerun`,
   with the mode and stages), plus a per-run budget cap and an expiry. Creating it is human-only
   (403 for agents and workers; the decision-actor rule of ADR 0065 applies). It validates what it can
   at that time: the spec parses and matches its digest, the cap fits the spec and, for a rerun, the
   lineage budget, and the source campaign is finished. It is the owner's "approved" mark. Only specs with
   such a record can be submitted by the loop; an unmarked spec is not reachable by any loop route.
2. **Consuming is the loop's action.** An enrolled agent with the new scope `campaign:submit_cleared` (named so that no scope or tool name contains "approve", which the MCP guard tests forbid)
   (or a person) calls submit on a pending record. The control plane creates the campaign and its
   approval row in one transaction, exactly as a standing start does. The approving principal is the
   human who created the record, with decision class `approved_submission` and automation label
   `approved_submission:<id>`. The consuming agent is never the approver and cannot change the digest,
   widen a cap, or create a standing approval, pre-approval, grant or budget raise.
3. **Checks before create, each a recorded refusal.** Operator opt-in enabled; kill switch not
   engaged; record pending, unexpired and at the expected revision; the creator still a live human with
   the same credential; the stored spec still matching its digest; the per-run cap within the spec's cap;
   the program cap (ADR 0045) and project spend headroom (as for standing starts) with room for the
   per-run cap; for a rerun, the source finished and the cap within the lineage's remaining budget. A
   refusal creates nothing. Temporary refusals (disabled, kill switch, headroom) leave the record
   pending; permanent ones (digest mismatch, expiry, creator changed) end it.
4. **One campaign per approval.** The record has a unique campaign id, the call is idempotent under the
   store's key rules, and a replay or a concurrent second call returns the same campaign.
5. **Off by default; kill switch honoured.** The operator enables the feature with
   `tessera serve --agent-submissions`, which requires `--agent-submission-kill-switch PATH`. Disabled, every submit is refused `feature_disabled` and recorded. When enabled, a
   marker file engages the kill switch; a missing or unreadable marker reads as engaged (fail closed), as in
   ADR 0085. The agent-side action also checks the loop's own kill switch before each call. The switch stops
   new submissions only; it does not touch campaigns that are running.
6. **Every decision is on receipts.** Each submit, refusal, cancel and expiry appends a decision row
   (append-only) and an event in the same transaction as the state change, naming the actor, reason and
   the checked caps. A refusal is never stored under the caller's idempotency key, so a temporary refusal cannot be
   replayed after its cause clears; the same key then submits. A repeated identical refusal for the same record is not appended again, so a polling
   loop does not fill the log. The resulting campaign's receipt carries a `submission` section only when a
   record exists.
7. **Visible and steerable over `tessera_mcp`.** Tools: list pending submissions and read a submission
   (`campaign:read`), cancel (`campaign:cancel`, reduce-only like other cancels), and submit an approved
   campaign (`campaign:submit_cleared`). A person can therefore see what the loop will do next and stop it
   before it happens.
8. **Enrollment.** The scope is granted only by its own opt-in (`--allow-campaign-submit-cleared` on the
   local enrollment helper) and is not part of `--allow-campaigns`.
9. **The loop action.** A small agent-side action lists pending records, checks the kill switch before
   each call, submits at most a configured number per tick with a key derived from the record and
   revision, records the outcome with the control plane's stable refusal reason, and never retries a
   refusal within a tick. A record that is refused does not hold up the next pending one: the action
   moves on, up to a configured number of attempts per tick, and only successful submissions count
   toward the per-tick limit. It refuses to run when `ANTHROPIC_API_KEY` is set, like the standing loop,
   and has no approve, create or widen operation. It reads the real list response, and a response of an
   unexpected shape is recorded as malformed and submits nothing. A contract test runs the action
   against the real control routes so a change in the response shape fails a test.
10. **The runner.** `tessera-submit-runner` calls the action on a schedule, so no session has to. It is
   manual-start and admission-gated like the reasoning unit, never enabled on boot, and reads its control
   endpoint and agent credential by reference (a token file), never by value. It refuses to start without
   a kill-switch marker path, and refuses to start when `ANTHROPIC_API_KEY` is set. While the kill switch
   is engaged it neither calls the action nor the control plane, and it never touches running campaigns.
   A tick fails when the action raises, when the list call fails or returns an unexpected shape, or when
   a submit raised an error and nothing in that tick succeeded (for example the control plane is
   unreachable). A failed tick backs off exponentially up to a cap and, after a configured number of
   consecutive failures, halts (exit 3) until an operator restarts it. A refusal with a stable reason is
   not a failure, and a tick that made progress is not failed by an error on another record. With the kill
   switch engaged the runner pauses without exiting and resumes by itself when the marker is removed. Every runner action is a line in a private, append-only journal that
   holds ids and stable reasons only; it is the runner's receipt log, next to the control plane's own
   decision rows. Only one runner may hold a journal. The control plane still has to be started with
   `--agent-submissions`; the runner cannot enable it. The runner makes no model call: a submission is a
   deterministic act on a record a person made, so the loop needs no model destination for it.
11. **Fixed terms.** The trust policy digest is fixed when the record is created, and a record can be valid
   for as long as its expiry allows (up to the standing-approval window). A rerun's source is checked
   as finished, not for unresolved exposure as a standing start does. This matches existing patterns and
   is a deliberate choice: the owner's record is the decision, and the platform does not re-decide it
   later except to refuse.

## Consequences

The owner approves once and the platform carries it out within the stated caps and the program budget,
with a receipt for each decision. A session no longer submits by hand. A rerun still needs the owner's
mark, so a failure cannot silently turn into repeated spend. The cost is one more record type and
scope to manage, and an operator switch to turn it on.

## Alternatives considered

- **Standing approvals only.** A standing approval already starts a spec within caps, and retries its own
  failures. It cannot rerun a campaign that did not start from one, has no pending queue an agent or
  person can inspect and cancel, and has no operator kill switch. This ADR reuses its start path and its
  budget checks rather than duplicating them.
- **Let the agent approve its own submission within the caps.** Rejected: an agent never approves or
  widens authority.
- **Submit from the loop with no human record.** Rejected: nothing would mark the spec as owner-approved.

## Tests

Control: create is human-only; consuming never makes the agent the approver; off by default and kill
switch fail closed; each refusal reason records a decision and creates nothing; one campaign per approval
under replay and concurrency; per-run, program and project caps; rerun lineage cap; cancel by agent and
human; decisions append-only; tenant isolation; the receipt section. MCP: tools absent without their scope.
Agent action: kill switch before each call, refusals not retried in a tick and not blocking the next record, API key refused, malformed list response submits nothing, and a contract test against the real routes. Runner: kill switch pauses all calls (an ambiguous marker counts as engaged) and resumes when cleared, backoff and halt including on submit errors, refusals are not failures, a private single-holder journal with no secrets, clean stop, refusal to start with an API key or missing references, and an end-to-end run through the real control routes. No live
provider or paid calls.

## Open questions

1. Whether a record may cover more than one campaign (a bounded count) rather than exactly one.
2. Whether the operator flag should live in the control server configuration or in the operator
   console, once that surface exists.
