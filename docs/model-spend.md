# Model spend budget (operator page)

Contract: [ADR 0090](decisions/0090-model-spend-ledger.md). Model-call spend (the reasoning
runtime's calls to an enrolled model destination) is a budget of its own: separate from campaign
compute, program, Salad and authority-grant budgets. Nothing here has been run against a live
model; costs are computed from the operator-pinned prices and are estimates, not invoices.

## Set limits (a person)

Nothing is spent until a person sets limits. Per project and destination (`*` is the project
default; a destination's own row wins):

```
PUT /model-spend/limits
{"project_id": "<project>", "provider_id": "anthropic", "per_call_cents": 5, "daily_cents": 100}
```

Human credential only; each change bumps the row's revision and records a `model_spend.limits_set`
event. A limit row for a destination that is not billed (an included-OAuth OpenAI destination) has no
effect: only billed destinations, today the Anthropic Messages API, go through the ledger. A per-call cap is checked against each call's worst case (input tokens at the input price
plus the output cap at the output price); the daily cap (UTC day) against settled spend plus open
reservations plus that worst case.

## Scopes

- `model_spend:record`: the reasoning manager credential. It reserves, settles and releases. Nothing
  else.
- `model_spend:read`: agents and MCP. Read totals only.

## Read totals

`GET /model-spend?project_id=<project>&days=7`, or the read-only MCP tool `tessera_model_spend`
(registered when the credential's live scopes include `model_spend:read`, or with
`--model-spend-tools`). It returns the project's limits, today's settled and reserved cents and call
count, the last `days` by day, totals by destination and the open reservations. It cannot change a
limit.

## Resolve an open reservation (a person)

An uncertain call (a transport failure or timeout, where the provider may or may not have accepted the
request) keeps its reservation open at the worst case, so it over-counts today's spend. It appears under
`open_reservations` in `GET /model-spend` (run, sequence, destination, model, worst case, day). Check
the provider's own usage view, then resolve it:

```
POST /model-spend/reservations/<call_id>/resolution
{"outcome": "not_sent", "reason": "provider usage view shows no request at that time"}
{"outcome": "spent", "actual_cents": 1.2, "reason": "usage view shows the call, 1.2 cents"}
```

`not_sent` releases the reservation (cost zero, resolution `person_released`); `spent` settles it at
`actual_cents`, which is required (resolution `person_settled`). `reason` is 1 to 500 characters. A
person's session credential is required: an agent credential, including the reasoning manager's, gets
403. The route is idempotent, refuses a call that is already settled or released, and records a
`model_spend.resolved` event with who, outcome, cents and reason. The operator-enabled MCP tool
`tessera_resolve_model_spend_reservation` (flag `--model-spend-resolve-tool`) calls the same route; the
API still requires a person.

## What happens when

| Situation | Result |
| --- | --- |
| Worst case over the per-call cap or what remains of today's cap | Refused before the call (`model_spend_cap_exceeded`, with the cap that failed); same model, nothing sent. |
| No limit for the destination or `*` | Refused (`model_spend_not_configured`). |
| A person then sets or raises a limit, or the UTC day rolls over | The next call is evaluated again: the supervisor uses a fresh reservation sequence after a refusal, because the control plane replays its answer for the same idempotency key. |
| Control plane unreachable, scope missing, answer malformed | The call is not made; the run backs off or halts. Fail closed. |
| API definitely refused the request (cap, rate limit, quota, egress) | The reservation is released; cost zero. |
| Outcome unknown (transport failure, timeout) | The reservation stays open at its worst case until a person resolves it (above). It over-counts, never hides spend. The run's `report` then shows it as resolved (the supervisor reads `GET /model-spend/reservations/{call_id}?run_id=...`, which answers an agent only for a call of its own run, and shows the amount a person recorded when they settled it); the run itself stays ended. |
| The answer to a reserve or settle was lost | The same request is retried with the same key (a hash of run, sequence and operation) and returns the stored result. |
| Calls to an unbilled destination | Not in the ledger or the totals; their cost stays on their receipts. |
| Success | The computed cost is settled and written on the call's receipt as `spend_ledger`. |

The model is never changed to a cheaper one because of any of these; a cheaper model is a new,
separately admitted profile. The supervisor's own journal caps stay on as defence in depth.

## Upgrading

Upgrade the control plane before the reasoning supervisor. The report refresh reads
`GET /model-spend/reservations/{call_id}?run_id=...`: a supervisor older than the run-scoped route
sends no `run_id`, gets a 422, and its report keeps the reservation rows it had (the refresh never
fails a run). Neither side needs to be upgraded at the same moment, but a person-resolved reservation
shows as resolved in the report only once both are current.
