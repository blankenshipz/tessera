# ADR 0061: campaign pause and resume, a lower-only concurrency limit, single-stage cancel

Status: accepted. Builds on ADR 0038 (campaign runner), 0045 (gates), 0053 (resolving a confirmed
exit) and 0055 (MCP steering tools), whose open list named all three.

## Context

An agent steering a campaign could cancel the whole campaign or lower its budget, but could not
hold it for a look, could not keep a many-stage campaign from using every worker slot, and could
not drop one stage that turned out to be wrong without cancelling the rest.

## Decision

Four tools and routes, no new enrolled action: each needs `campaign:cancel`, only the submitting
agent (or a person) may use it, the control API re-checks the scope, and each is revision-guarded
and idempotent like `tessera_reduce_campaign_budget`. They apply only while the campaign is
`running` or `stopping`; a delegation-lapsed, blocked or finished campaign is refused.

1. **Pause / resume** (`tessera_pause_campaign`, `tessera_resume_campaign`;
   `POST /campaigns/{id}/pause`, `/resume`). A pause stops NEW stage reservations: the runner does
   not prepare or reserve a pending stage, nor re-reserve one after a transient failure. Attempts
   already running are not touched: they finish, settle and reconcile as usual. Cancel, the budget
   cap, the approval's lifetime and the deadline are unchanged, so a long pause can run a campaign
   out of its deadline. A pause set by a person can be lifted only by a person; an agent's pause may
   be lifted by the agent or a person. Resume restores only what the approved spec already allowed.
2. **Concurrency limit** (`tessera_limit_campaign_concurrency`;
   `POST /campaigns/{id}/concurrency-limit`). A cap, 1 to 64, on how many of the campaign's stages
   may have an unfinished attempt at once; another stage waits at reservation. It only lowers: the
   first limit may be any value (no limit to a limit is a reduction), a later one must be lower.
   Raising it, and the project's own concurrency, stay the operator's. It narrows, never widens,
   the project limits.
3. **Single-stage cancel** (`tessera_cancel_campaign_stage`;
   `POST /campaigns/{id}/stages/{stage}/cancel`). Records a request; the runner then unwinds that
   stage with the same grant revoke, reconcile and settlement path as a campaign cancel (a grant no
   worker leased is released once it expires). The stage ends `cancelled`, stages that read it are
   skipped like any dead dependency, and when nothing else failed the campaign ends `stopped` with
   reason `stage_cancelled`, not `failed`. A finished stage and a second request are refused.

State is two additive tables (`campaign_steering`, `campaign_stage_cancels`, schema 71), created
only when a campaign is steered. Every change is an event (`campaign.paused`, `campaign.resumed`,
`campaign.concurrency_limited`, `campaign.stage_cancel_requested`, `campaign.stage_cancelled`;
the runner records `campaign.stage_waiting_paused` and `campaign.stage_waiting_concurrency` once
per stage and pause or limit). A steered campaign's terminal receipt carries a `steering` section
(pause count, whether it finished paused, the concurrency limit, each stage cancel with its
times); an unsteered campaign's receipt, and so its digest, is unchanged.

## Consequences

- Row 6 of [docs/mcp-steering.md](../mcp-steering.md) is closed. Extending a deadline, raising a
  cap or limit, approval and grant reconciliation remain human.
- Pausing is not a safety stop: it does not revoke running work. Use cancel for that.
- A cancelled stage is a recorded decision, not evidence that the experiment failed; the receipt
  keeps the scientific verdict `not_assessed`.
