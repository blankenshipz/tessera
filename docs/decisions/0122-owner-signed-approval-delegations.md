# 0122 — Owner-signed delegations: approve campaigns and record gate decisions by a written rule

## Status

Implemented. Schema 86 (additive: two tables and a `delegation_id` column on campaign approvals and
gate decisions). Builds on the campaign approval and gate decisions of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and
[ADR 0045](0045-campaign-gates-program-budget.md), standing approvals
([ADR 0042](0042-standing-approvals-and-scheduled-campaign-starts.md)) and approved submissions
(ADR 0143). It keeps the authority rule of DESIGN §5.3: no credential widens its own authority.

## Context

The launch docs said "a human approves every campaign and records every gate decision". For work that
is routine and bounded (a line of research the owner has already decided to fund within a ceiling) that
rule makes the owner a relay: the same person re-approves campaigns that fall inside a decision already
made. The platform already lets a person pre-decide narrower things (a standing approval for one spec, an
approved submission for one digest). It has no way to write down a *rule* once and let the platform apply
it, with the owner asked whenever a request falls outside it.

## Decision

1. **A delegation is a record only a person signs.** `POST /projects/{id}/approval-delegations` (human
   only; an agent, a worker or an external-scope credential gets 403) stores closed, immutable terms:
   - `program_id` (a program the operator configured, which must cover the project),
   - `budget_ceiling_cents` (the most the delegation may commit in total),
   - `per_run_cap_cents` (the most one approved campaign may hold),
   - `allowed_providers` (every provider a stage names, with its alternates and reroute targets, must be
     listed),
   - an optional `max_approvals`, and
   - `gate_rules`: each names a pre-registered paired-delta outcome (the same bound outcome an unlock
     requirement uses) and the decision a pass and a fail record.

   It expires within 30 days, can be revoked by a person, is never edited, and its terms digest is on
   every decision. No tool or route lets an agent create, revoke or change one.
2. **Who may use it.** A human, or a non-human credential enrolled with the separate scope
   `campaign:decide_delegated` (not part of `--allow-campaigns`). Workers never.
3. **A delegated approval** (`POST /campaigns/{id}/delegated-approval`) approves a campaign that is
   `submitted` and only as its first approval, when every rule holds *at that moment, under the write
   lock*: the delegation is active and unexpired; the person who signed it is still a live human of the
   project with the same credential; the spec's cap is within the per-run cap; what the delegation has already committed plus this cap is within the
   ceiling (counted by the program budget's own rule, cancelled campaigns included: a finished
   campaign counts what it held and settled, any other the larger of its cap and what it holds and
   settled, so cancelling a campaign that spent never frees its spend); the number of campaigns
   approved under it, cancelled ones included, is within `max_approvals`; every provider is allowed; the program covers the project; and the usual
   admission checks (spec references, trust, program budget) pass. It carries no unlock requirement and
   no Salad review pre-approval, so a spec that needs a person for those is refused (unless the
   delegation carries the optional review rule of [ADR 0150](0150-deferred-launch-delegated-preclear.md)). The approval row
   records the signing person as the approver (the runner acts as, and only while, that person is live),
   the class `owner_delegated` and the delegation id; the decision events carry the same.
4. **A delegated gate decision** (`POST /campaigns/{id}/delegated-gate-decision`) records a go or no-go
   only when the platform computes the named rule's outcome from recorded metrics (never from a number
   the caller supplies) and the rule maps that outcome to a decision. An unevaluable result, an
   unmapped outcome, a campaign that is not finished or has unresolved spend, and an existing no-go veto
   all refuse. A delegated go is accepted by a later unlock requirement like a person's go: any requirement
   that accepts a human go accepts it, **without** that requirement's own bound outcome and
   without the opt-in an automation-labelled owner go needs, because the signed rule stands in for
   the person. A delegation whose gate rule is weaker than an unlock requirement's therefore
   satisfies it; the owner who signs a gate rule accepts that, and no unlock requirement can
   demand more of a delegated go than the rule says. a no-go is a
   veto like any other.
5. **Outside the delegation it fails closed and asks the owner.** A request that is not covered returns
   409 `outside_delegation` with stable reason codes (never an id or a value from the spec), changes
   nothing, and is recorded once as an `escalated` decision (`GET /projects/{id}/delegation-escalations`
   lists them). A stale revision or digest is an ordinary conflict, not an escalation.
6. **Every decision is on a receipt that cites the delegation.** The campaign receipt gains a
   `delegation` section (each decision's kind, outcome, rule, the delegation id and its terms digest,
   the numbers that were checked) and the approval carries `delegation_id`; a delegated go is marked in
   the unlock section. Existing receipts keep their shape when no delegation was used.
7. **Not done here.** The control plane does not approve on its own tick: a decision needs a caller (a
   person or an enrolled agent). Delegations never cover raising a cap, changing a spec, an unlock or
   pre-approval, pausing, cancelling or any provider or trust change; those stay a person's.

## Consequences

- An owner states the rule once; routine campaigns inside it need no per-campaign human step, and
  anything outside it reaches the owner as a listed escalation.
- Authority is bounded by what the owner signed and re-checked every time: revoking it, letting it
  expire, or the signer losing their credential stops it at the next request.
- The signing person stands behind the approvals made under their delegation: the runner acts as them.
- A gate rule trusts the platform's own computation of a pre-registered outcome, not an agent's claim.

## Tests

`tests/test_approval_delegations.py` (the record, triggers, migration, routes), `tests/test_delegation_rules.py`
(every reason code and the gate rules), `tests/test_delegated_decisions.py` and
`tests/test_delegated_decisions_api.py` (approve, ceiling, escalation, replay, scope, gates, receipts),
`tests/test_mcp_delegated_decisions.py` and `tests/test_enroll_local_mcp_delegated.py` (the MCP tools and
enrollment).
