# 0162 — First-call readiness reads the spend caps and the grant through the control API

## Status

Implemented. No schema change and no migration. Amends [ADR 0109](0109-reasoning-first-call-readiness.md)
(the `spend_caps` and `grant` checks read the control database) and builds on
[ADR 0155](0155-reasoning-only-grant.md) and [ADR 0090](0090-model-spend-ledger.md).

## Context

`tessera-reasoning-readiness check --in-unit` runs as the reasoning service user inside a transient
unit. That user cannot read the control database (rightly: it is the control plane's), so `spend_caps`
and `grant` stayed `unchecked` and one in-unit run could never exit 0; the operator had to run a second
check as a user that can read the database. An unverified spend limit or grant is exactly what makes
the first call fail after the key is installed, so neither may be assumed.

## Decision

1. **A database-free read.** When `--control-url` is given and `--control-state-dir` is not,
   `spend_caps` and `grant` are evaluated from two read-only GETs made with the service's own
   `agent-token` credential (the file the unit already loads, read from the manager credential
   directory under the same private-file rules as the profile) and its `X-Agent-Destination`:
   `GET /model-spend/limits?project_id=...` and `GET /agent-authority-grants/{id}`. The token is
   sent only to those two reads, never to the unauthenticated slice probes, and is never printed,
   logged or placed in a finding. Redirects are not followed and no proxy is used.
2. **The same verdicts.** The caps rule and the grant rule are one function each for both paths, so
   every existing finding code (`grant_missing`, `grant_revoked`, `grant_expired`,
   `grant_lineage_inactive`, `grant_revision_mismatch`, `grant_scope_mismatch`,
   `grant_budget_exhausted`, `grant_budget_below_call_cap`, ...) and every fix text is unchanged.
   The grant's lineage is the API's `effective_status` (`active`, else `fenced`) and its remaining
   budget is the API's `remaining_budget_cents`, both computed by the control plane.
3. **Fail closed.** The database path is used whenever `--control-state-dir` is given, as before.
   Without a readable `agent-token` (or with an invalid control URL) the checks stay `unchecked`.
   An unreachable control plane, a 401/403, an oversized, non-JSON or wrongly shaped answer, and a
   missing field are `fail` (a grant answer that does not parse is `grant_malformed`); a 404 for the
   grant is `grant_missing`. Nothing unread is reported `ok`. An execution-kind grant's bindings are
   execution grants the agent credential cannot read, so that kind through the API is `unchecked`
   (use `--control-state-dir`); the reasoning kind, which has no bindings, is fully checked.
4. **One minimal read route.** The service credential holds `model_spend:record`, not
   `model_spend:read`, so `GET /model-spend` (which returns totals and open reservations) was refused to
   it. `GET /model-spend/limits?project_id=` is added: a human, or an enrolled agent with
   `model_spend:record` or `model_spend:read`, in the project's own organization and project, receives
   the project's caps and nothing else (no totals, calls or reservations). It changes nothing; `PUT`
   on the same path stays human only. The unauthenticated slice probe of this path keeps seeing a
   route that exists (401, 403, 405 or 422). The grant read needs the existing `authority:consume`
   scope and the grant's own subject, as the runtime already does.

## Consequences

- One `tessera-reasoning-readiness check --in-unit --control-url URL` run, with no database path, can
  exit 0 (ready) or 10 (ready except the key).
- No new scope, no new write, and no relaxation of any refusal: the route is tenant- and
  scope-checked and the readiness verdicts are the ones the database path already produced.
- A reasoning service enrolled without `model_spend:record` or without `authority:consume`
  reports the missing scope in the fix text instead of passing.

## Addendum: execution-grant bindings through the API

Point 3 above left an execution-kind grant `unchecked` through the API. It is now checkable by one
narrow addition: `GET /agent-authority-grants/{id}` adds `bindings_active` (a boolean, execution
kind only) computed by the control plane in the same read transaction. It is true only when every
bound execution grant, looked up in the agent grant's own project, is active at the pinned revision
and epoch and unexpired, the rule the database path applies. The route's existing checks are
unchanged (organization and project, `authority:consume`, and the grant's own subject for an agent),
so no execution grant is made readable: the subject learns one boolean about grants its own bounds
already name. A missing, non-boolean or absent field (an older control plane) stays `unchecked`
with text saying so, never `ok`; `false` is `grant_binding_inactive`. The reasoning kind has no
bindings and gets no field.
