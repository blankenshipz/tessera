# 0155 — A reasoning-only agent authority grant

## Status

Proposed. Amends the closed request shapes of [the agent authority interface](../agent-authority-interface.md)
and builds on [ADR 0142](0142-grant-budget-debited-by-model-spend.md) (a grant's budget is spent by the
model calls it pays for) and [ADR 0090](0090-model-spend-ledger.md). Related: [ADR 0027](0027-external-agent-adapter.md).
No schema change and no migration.

## Context

A reasoning run (plan critiques, research and review jobs, steering through MCP) names the agent
authority grant that pays for it. Until now the only grant shape was an execution one: issuing a grant
requires at least one binding, and each binding must name an execution grant that is active, at its
expected revision and epoch, and unexpired. With no experiment campaign running there is no execution
grant, so no agent grant could be issued, and the agent loop could make no receipted model call. The
agent loop therefore depended on an experiment being mid-run, which it should not.

The binding protects execution-side rules only: agents never mint execution authority (they consume a
separately human-issued execution grant pinned to a variant, task, plan and target digest); no running
grant or lease outlives the agent authority; and revoking the agent grant cancels the execution it
consumed (see the interface note, "Consumption" and "Execution grants are separately human-issued").
None of these concerns a model call. ADR 0142 made model spend a debit on the same grants, so reasoning
inherited a binding it does not need.

## Decision

1. **A grant has a kind.** `bounds.kind` is `execution` (the default when absent, so every stored grant
   and every older client keeps its meaning) or `reasoning`. The kind lives in the bounds JSON, which is
   written once and never updated. There is no migration.
2. **A reasoning grant authorizes only model-runtime calls.** Its `actions` are exactly `["model:call"]`,
   its `bindings` are empty and its `delegation_depth` is 0. It names the `provider_id` and the bare
   `model` it pays for. It authorizes no stage launch, no execution, and no provider effect.
3. **What the grant does not control.** The egress limit to the registered model host is enforced by the
   model destinations and the readiness egress allowlist, not by the grant. The grant's `destination`
   field is the agent's own identity destination (`X-Agent-Destination`), not the model host. The grant is
   a spending and authorization record, not a network control.
4. **Bound to spend limits and the grant debit.** Issuance is refused (`model_spend_not_configured`)
   unless the project has a usable model-spend limit row for the provider (or the `*` default). The grant
   has its own `budget_cents`, at least 1 cent, and an expiry at most 24 hours ahead and not past the
   subject credential's expiry. Every reservation names the grant, so ADR 0142's debit applies: the
   project's per-call and daily caps and the grant's remaining budget both have to allow the call.
5. **Who may issue it.** A human session, or the owner token carrying an automation label, through the
   existing route `POST /projects/{id}/agent-authority-grants` with the existing idempotency. The
   subject credential must hold `authority:consume` and `model_spend:record`. The approval is recorded as
   the `agent_authority.issued` decision event (now carrying the kind) and its provenance record.
6. **Every call is on receipts.** Each call has its model-spend ledger row, its link to the grant
   (ADR 0142) and the runtime's own receipt; the grant itself never writes consumption rows.

## Fail-closed rules

- No spend limit for the project and provider: issuance refused (`model_spend_not_configured`).
- Budget below 1 cent: refused at issuance; a zero remaining budget blocks the runtime.
- Expired grant: refused at the runtime fence and by the ledger (`grant_not_active`).
- Consume: refused. A reasoning grant cannot consume any execution action.
- Delegation: refused, both from a reasoning parent and across kinds.
- Runtime-bindings enrollment: a reasoning grant cannot be the parent of one, because native operations
  are provider effects.
- Scope mismatch: the ledger refuses (`grant_scope_mismatch`) a reservation whose provider or model is
  not the one the grant names.
- Revoked lineage: the ledger now checks the grant's ancestry as well as its own status
  (`grant_not_active`), for every kind.
- Unknown kind or missing `model:call`: the runtime fence blocks the run before any call.
- Tools: with tools on, a reasoning run is blocked unless every tool the harness would offer is
  declared read-only (`tool_not_read_only`).

## What it does not do

It cannot escalate to execution. The revoke cascade and the execution fences look only at execution
consumptions, so revoking a reasoning grant never touches an execution, and a reasoning grant never
holds, extends or cancels one. It does not widen any existing grant, and the execution model is not
relaxed (its bindings stay required).

## Escalation risks and how each is closed

| Risk | Closure |
| --- | --- |
| An empty-binding execution grant used as a parent or carrying attempt actions | The execution shape is unchanged and still requires bindings; only the explicit `reasoning` kind has none. |
| A reasoning grant as parent of native provider operations | Runtime-bindings enrollment refuses a reasoning parent. |
| Cross-kind or onward delegation | Delegation refuses a reasoning parent and a child of a different kind. |
| Tools run as the agent credential's own scopes and could cause effects | The read-only tool gate; the reasoning credential should be enrolled with minimal scopes. |
| A reasoning grant paying for a different model or provider | The ledger's scope match on provider and model. |
| A revoked ancestor not seen at the ledger | The ledger checks lineage for every kind. |
| A reservation that names no grant, capped only by project limits | Unchanged from ADR 0142: the runtime always names the grant. |
| Readers that index `bindings` or `actions` on every grant | A reasoning grant always stores `bindings: []` and `actions: ["model:call"]`; agent-to-agent paths then fail closed. |
| Budget 0 | Required at least 1 cent for this kind. |

## Consequences

The agent loop can make its first receipted model call with no experiment running. A person (or the
labelled owner) still decides, per grant, how much may be spent and for how long. The cost is a second
shape in the bounds union and several refusals on paths that previously assumed every grant is an
execution grant. Operators issue it through the API, the `tessera authority issue-reasoning` command
or the person-only MCP tool, and the first-call readiness `grant` check confirms, read-only, that the profile's grant exists, is active, unexpired and not lineage-revoked, matches the profile's revision, epoch, subject, destination, model and project (and, for the reasoning kind, the provider, `model:call` only and no bindings), and has at least the per-call cap left.

## Alternatives considered

- **A dedicated kind column with a migration.** Rejected for now: the bounds are written once and never
  updated, nothing queries by kind, and a migration would need a fingerprint update for no new guarantee.
  It can be added later without changing the contract.
- **Relaxing the binding requirement for every grant.** Rejected as unsafe: an execution grant with no
  binding could carry attempt actions or parent runtime bindings, and the execution invariants that
  bindings protect would no longer be implied by the shape.
