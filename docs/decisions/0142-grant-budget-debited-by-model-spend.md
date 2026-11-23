# 0142 — A grant's budget is spent by the model calls it pays for

## Status

Proposed. Schema 88, the step after the guardian record (87). The migration number reserved from the
shared allocator (allocator number 99) need not equal the schema version, which is the next free contiguous step on
main. Builds on the model-spend ledger of [ADR 0090](0090-model-spend-ledger.md) and the agent
authority grants described in [the agent authority interface](../agent-authority-interface.md).
Changes no provider code.

## Context

A reasoning run reserves its worst case on the model-spend ledger, calls the model and settles the
actual cost there. The agent authority grant that authorizes the run carries a budget, but nothing
consumed it: `remaining_budget_cents` never moved, and the runtime's fence compared only the current
run's own cost with that unchanging number. Sequential runs, each within the grant on its own, could
therefore bill past the grant's budget, and a person who issued a grant with a small budget had no
real bound.

## Decision

1. **A call names the grant that pays for it.** The reservation request takes an optional `grant_id`.
   The reasoning runtime sends its grant's id. A reservation without one behaves as before (ledger caps
   only).
2. **The link is written with the reservation, in the same transaction.** `model_spend_grant_links`
   (append-only, schema 88) holds one row per call: the call id, the organization and the grant. The
   reservation is refused, with no ledger row and no link, when the grant is not the caller's own, in
   this project and in this organization (reads as not found otherwise), is revoked or expired
   (`grant_not_active`), or has less budget left than the call's worst case (`grant_budget_exceeded`,
   with the remaining budget). The grant check is judged together with the ledger's own caps; either
   refuses, and a refusal is recorded as a `model_spend.refused` event.
3. **The grant's spend is derived from the ledger rows it links.** An open reservation counts its worst
   case, a settled call its settled amount, a released call nothing. Settling, releasing and a person's
   resolution therefore move the grant's remaining budget in the same statement that moves the ledger
   row; there is no second write that can drift, and a retried or replayed call cannot erase a real cost.
   A settlement above its reservation is counted at the settled amount, so remaining budget can reach
   zero and the next reservation is refused.
4. **One remaining budget.** Remaining budget is the grant's budget minus its execution and delegation
   holds (`reserved_cents`) minus that model spend, in exact micro-cents; the read shows it in cents
   (whole cents as an integer, as before, else the exact fraction). The same figure bounds an
   attempt-admission charge and a delegated child grant's budget, so model spend cannot be spent twice.
5. **The runtime fence uses it.** The pre-call check compares the worst case with the live remaining
   budget. With the ledger on, the remaining budget already contains this run's calls, so the runtime no
   longer adds the run's own cost to it; without the ledger it still adds only that run's cost. A grant
   refusal is a definite refusal: the call is not made, nothing is reserved, and the next attempt uses a
   new sequence.

## Consequences

Sequential runs on one grant spend one budget. The cost is one append-only table, one optional field,
and a remaining-budget figure that now reads the ledger. A grant spent only by calls that never name it
(an older client) is not bounded by it; the runtime always names it.

## Alternatives considered

- **Increment `reserved_cents` at reserve and true it up at settle.** That column is whole cents, so
  each sub-cent call would round up and a run of small calls would exhaust the grant early; it also
  needs a second write at settle, release and resolve.
- **A per-call debit table with its own status.** It duplicates the ledger's state machine and can drift
  from it. Deriving from the ledger rows cannot.

## Tests

`tests/test_first_call_real_grant.py` against the real routes: a budget below the worst case is refused
with no billed call and no ledger row; the ledger refuses a reservation the grant cannot cover and sees
live remaining budget; a budget just large enough is admitted and debited by the settled amount;
sequential runs stop before the grant is overspent; a release gives the budget back; another grant or a
revoked one cannot pay; model spend is taken out of what a grant may delegate.
`tests/test_grant_model_spend_migration.py` and the schema fingerprint cover schema 88.
