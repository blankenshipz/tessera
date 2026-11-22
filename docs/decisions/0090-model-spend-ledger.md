# ADR 0090: model spend is a budget of its own, settled in the control plane

Status: accepted and implemented. Adds a control-plane ledger (a migration at the end of the
schema chain). Builds on [ADR 0082](0082-agent-loop-anthropic-destination.md) (enrolled model
destinations), [ADR 0083](0083-model-call-receipts.md) (one receipt per model call; caps before the
call; back off, never downshift) and the existing agent authority grant budget. Compute and Salad
budgets ([ADR 0045](0045-campaign-gates-program-budget.md)) are unchanged.

## Context

The reasoning supervisor computes each model call's cost in cents from operator-pinned prices and
writes it on the call's receipt (ADR 0083). It fenced that cost only inside its own private journal:
the per-call and daily caps lived next to the process that spends, and the control plane never saw
the spend. Two consequences: nothing a person could set or read in Tessera said how much a project
had spent on models, and a second supervisor, or a lost journal, started from zero.

## Decision

1. **A separate model-spend budget.** It is distinct from a campaign's compute budget, a program
   budget, Salad exposure and an authority grant's `budget_cents`. Model spend never draws from, or
   is hidden inside, those, and they do not cap it.
2. **Limits are explicit operator data.** A person sets, per project and per model destination
   (the provider id of the enrolled destination, or `*` for the project default), a per-call cap and
   a UTC-daily cap in cents (`PUT /model-spend/limits`, human only, revisioned, audited as a control
   event). A limit for the destination wins over `*`. With no limit for either, every call is
   refused (`model_spend_not_configured`): nothing is spent until a person decides how much.
3. **Reserve before, settle after, release if never sent.** Before a billed call the supervisor asks
   the control plane for a reservation of the call's worst-case cost (input tokens at the input price
   plus the output cap at the output price: the same preflight ADR 0083 already computes). The
   control plane refuses (`model_spend_cap_exceeded`, with the cap that failed) unless the worst case
   is within the per-call cap and within what remains of the UTC day's daily cap, counting settled
   spend plus every open reservation. After the response the supervisor settles the cost computed
   from the reported usage and the pinned prices; a request the API definitely refused (a cap, a
   rate limit, quota, an egress refusal) is released and costs nothing. A call whose outcome is
   unknown (transport failure, timeout) is never released: its reservation stays open at its worst
   case until a person resolves it (decision 9), so uncertainty can only over-count, never hide spend.
   A refused reservation is re-evaluated: the control plane replays its stored answer for an
   idempotency key, so a refusal would otherwise stick for ever, and the supervisor therefore uses a
   fresh reservation sequence after every refusal. Once a person sets or raises a limit, or the UTC day
   rolls over, the next call is evaluated again. A lost answer (a transport failure) is retried with the
   same key and returns the stored result. The key is a hash of the run, the sequence and the operation,
   so a long run id cannot truncate it.
4. **Fail closed, never downshift.** If the control plane cannot be reached, the credential lacks
   `model_spend:record`, or the answer is malformed, the call is not made and the run halts or backs
   off with the same model. A refusal never selects a cheaper model; a cheaper model is a new,
   separately admitted profile (ADR 0083). The supervisor's own journal caps stay in force as defence
   in depth; the control plane's are an additional, authoritative check.
5. **Every settlement is on a receipt.**
   - **The receipt row.** Each call's receipt row gains a `spend_ledger` object (`call_id`, `status`,
     `settled_micro_cents`, `over_reservation`, `cap_exceeded`). The settlement carries the sha256 of the
     receipt row without that object, so the ledger and the receipt name each other. The control plane's
     own record of the call holds the reserved amount, the UTC day, the project and the provider.
   - **Events.** The control plane records `model_spend.reserved`, `.refused`, `.settled`, `.released`,
     `.resolved` and `.limits_set`. A refusal carries the code and the cap that failed.
   - **The research journey receipt** carries each call's `spend_ledger` field, because it carries the
     model-call rows. It does not read the run's reservation list.
   - **The run's reservation list** (including open and refused reservations) is in the supervisor's own
     `run`/`report` output. For each reservation it still holds as open, the supervisor reads the current
     status from the control plane (`GET /model-spend/reservations/{call_id}?run_id=...`, read-only, and
     answered only for a call of the caller's own run). A reservation a person resolved is then shown as
     resolved, with its resolution and, when a person settled it, the amount recorded.
   - **The refresh never changes the run.** An unreachable control plane leaves the rows as they were, and
     the run's state and halt reason never change because of the refresh.
6. **Generic for any billed destination.** The ledger is keyed by the destination's provider id and
   knows nothing about a vendor. Only billed destinations go through it: today the Anthropic Messages
   API (the default), billed from operator-pinned prices. The optional OpenAI included-OAuth
   destination is not billed; its calls are not reserved or settled in the ledger and do not appear in
   the totals (its reported cost stays on its receipts, as before), and a limit row for such a
   provider has no effect. Another billed destination added to the registry later uses the ledger
   unchanged.
7. **Integer micro-cents.** The ledger stores cost as integer micro-cents (one cent is 1,000,000)
   so sums are exact. Cost is an estimate from operator-pinned prices, not a vendor invoice, and the
   operator keeps the prices current.
8. **Visible.** `GET /model-spend` (a person, or an agent with `model_spend:read`) returns the
   project's limits, today's settled and reserved totals, the last days by day, totals by
   destination and the open reservations; the `tessera_model_spend` MCP tool is a read-only view of
   it. Neither can change a limit.
9. **A person resolves an open reservation.** An open reservation (an uncertain call) is listed under
   `open_reservations` with its run, sequence, destination, model, worst case and day. A person
   decides from the provider's own usage view whether the request was accepted:
   `POST /model-spend/reservations/{call_id}/resolution` with `{outcome, actual_cents, reason}`,
   where `outcome` is `not_sent` (the reservation is released, cost zero, resolution
   `person_released`) or `spent` (it is settled at `actual_cents`, which is required, resolution
   `person_settled`), and `reason` is 1 to 500 characters. The route needs a person's session
   credential: an agent credential, including the supervisor's, gets 403. It is idempotent, refused
   for a call that is already settled or released, and recorded as a `model_spend.resolved` event
   carrying who resolved it, the outcome, the cents and the reason. An operator-enabled MCP tool,
   `tessera_resolve_model_spend_reservation` (flag `--model-spend-resolve-tool`), calls the same route;
   the API still requires a person.

A released reservation may be reserved again: the call row carries an `attempt` number, so a retry after
a definite not-sent rejection is accepted under the same run and sequence (the supervisor also advances its
own reservation counter). A reservation answer is HTTP 200 with `allowed: false` and a code when a cap or a
missing limit refuses it, so a caller reads the body, not the status.

Deploy order: upgrade the control plane before the supervisor. A supervisor that does not send `run_id` to
the run-scoped status read gets a 422; the report refresh swallows it and keeps its rows, so mixed versions
degrade only the report's view of a person-resolved reservation.

## Consequences

- A person can cap model spend per project in Tessera and see it, and a restarted or second
  supervisor shares one daily total.
- The control plane is now on the path of every billed model call; its outage stops billed calls
  (intended: fail closed).
- A new action scope pair, `model_spend:record` (the reasoning manager credential) and
  `model_spend:read` (agents, MCP), is part of the external agent scope list.
- Uncertain calls over-count until resolved by a person through the resolution route.
- Calls to an unbilled destination are not in the ledger; their cost stays on their receipts.

## Limits

- No live model call was made to build or test this; tests use fakes and no network or key.
- Prices are operator-pinned and not verified against a provider invoice.
- Daily totals are by UTC day across the project, not per credential.
- The supervisor's local journal is not reconciled with the ledger after the fact; a call settled
  locally but never settled in the control plane stays an open reservation until resolved.

## Tests

`tests/test_model_spend_store.py` and `tests/test_model_spend_api.py` (limits, reservation caps
including open reservations, settlement, release, fail closed, tenant isolation, immutable history,
events, migration), `tests/test_model_spend_integration.py` (the supervisor's client against the real
routes), `tests/test_reasoning_model_spend.py`
(the supervisor reserves before, settles after, releases on a definite refusal, halts when the ledger
is unavailable, and carries `spend_ledger` on the receipt), `tests/test_mcp_model_spend.py` (the
read-only tool and its scope-gated registration).
