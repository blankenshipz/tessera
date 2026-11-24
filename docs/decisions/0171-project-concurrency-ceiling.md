# ADR 0171: the project concurrency limit is bounded by an operator ceiling

Status: Implemented (schema 89). Builds on the execution admission contract in
`docs/execution-admission.md`, ADR 0048 (grant lifetime and slots) and ADR 0153 (stuck campaigns
release capacity).

## Context

A project's `max_concurrency` (how many execution grants may be unresolved at once) was fixed at
1 to 8 in three places: the request model of `PUT /projects/{id}/execution-limits`, the database
CHECK on `execution_project_limits.max_concurrency`, and the documentation. A project that has
all 8 slots taken by grants a person still has to reconcile cannot admit even a small smoke
campaign, and nobody could raise the bound without a code change.

## Decision

1. **An operator ceiling.** `TESSERA_MAX_PROJECT_CONCURRENCY` in the control service's
   environment sets the ceiling. Unset (or empty) it is 8, so behaviour is unchanged. It is read
   once, when the app is built (`concurrency_ceiling.operator_concurrency_ceiling`).
2. **Fail closed.** A value that is not plain ASCII digits, or lies outside 1 to 64, stops the
   control service at start with a clear error. It never falls back to the default or to any
   other value.
3. **A hard cap of 64.** Eight times the default, well past one host's useful parallelism, and
   small enough that the per-admission count of unresolved grants stays trivial. The request
   model and the database CHECK use the hard cap; raising it needs another migration.
4. **Not agent-raisable.** Only the operator's environment sets the ceiling. No API field, MCP
   tool, campaign spec or stage input reaches it, and the route that sets a project's
   `max_concurrency` remains human-only.
5. **Checked where the request is accepted.** The store refuses a `max_concurrency` above the
   ceiling inside the same transaction as the tenant check and the idempotency record (HTTP 422,
   naming the ceiling). A stored limit above a since-lowered ceiling admits new grants, and
   campaign placement, only up to the ceiling; grants already issued, their leases and slot
   release are untouched.
6. **Evidence.** When the operator set the variable, the `execution.limits_configured` event
   records `concurrency_ceiling`. When it is unset the field is absent, which means the default
   8, so the events, responses and receipts of existing projects and campaigns are unchanged.
7. **Schema 89** rebuilds `execution_project_limits` with the CHECK `BETWEEN 1 AND 64`, copying
   every row unchanged and refusing to migrate if the copy differs.

## Consequences

An operator can give a project more room (for example 12) by setting one variable and
restarting control; a person then raises the project's limit through the usual route. Worker
slots (`worker_slots`, 1 to 8) and a campaign's own concurrency limit are separate bounds and
are unchanged.
