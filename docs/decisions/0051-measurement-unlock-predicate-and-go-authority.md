# 0051 — Measurement predicates on unlock requirements and opt-in automation go

## Status

Implemented (no schema change). Extends the unlock requirements of
[ADR 0045](0045-campaign-gates-program-budget.md).

## Context

An unlock requirement could be satisfied by a human gate `go` or by a platform-computed
paired-delta outcome. A stage that records a numeric verdict could not influence it, and a
`go` recorded by an owner credential under a self-declared automation label never counted,
so a chain of campaigns always stopped for a person even where the operator had decided
that an owner-run script may stand in for one.

## Decision

1. **Measurement condition.** `UnlockRequirement.measurement` is an optional
   `{stage, name, op, value}` (`op` in `lt le gt ge eq`, `value` finite). It is evaluated in
   the same admission transaction as the rest of the requirement, from the named campaign's
   immutable terminal receipt, and it only narrows: the requirement's go or bound outcome
   must still hold, and every budget, program-cap, trust and authority check is unchanged.
   It fails closed with a stable reason (`measurement_receipt_missing`,
   `measurement_stage_missing`, `measurement_attempt_not_unique` (not exactly one succeeded
   attempt), `measurement_not_recorded`, `measurement_not_numeric`,
   `measurement_condition_false`). An absent condition leaves every requirement digest and
   stored evaluation unchanged.
2. **Go authority.** `tessera serve --accept-automation-gate-go` makes a `go` recorded by the
   owner credential under an automation label (class `owner_automation_labelled`) count as
   the go of an unlock. The default is unchanged: only a person's session counts. The
   unverified `desktop-owner` label never counts, a later `no_go` still vetoes, the decision
   row keeps its class, and the receipt names the decision, so a script's go is never
   presented as a person's. Recording a decision still needs the owner credential and its
   automation label; agents, workers and external-scope credentials still cannot.

## Consequences

- A measured verdict can narrow any requirement: together with a go, or together with a
  bound outcome (which needs no go and no opt-in). It never replaces either. Counting an
  owner credential's automation go happens only where the operator opted in.
- The condition reads recorded numbers; it is not scientific validation (`not_assessed`).
- The opt-in is process-wide and read at service start, like the program budget.
