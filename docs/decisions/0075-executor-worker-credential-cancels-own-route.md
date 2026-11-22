# 0075 — The executor's worker credential cancels its own routes

## Status

Implemented (no schema or migration change). Amends the cancel credential of the live-check
config in [ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and the executor enrollment of
[ADR 0056](0056-executor-enrollment-into-an-existing-project.md). The public-internet
invariant of [ADR 0047](0047-agent-routed-data-destinations.md) is unchanged.

## Context

The Salad live check cancels a route on failure or at the end of a run. Route `cancel` was
a control command that only a human credential could send, so the live-check config named a
`cancel_credential_file` holding a project `human_session` credential. That credential
lasts at most 24 hours with no renewal path, so a long-lived executor service ends up with
an expired cancel credential. Making it needs a human session, which an agent must not
issue, and in a shared executor project it reaches every route in the project.

## Decision

1. **The worker cancels, nothing else.** `RemoteExecutionStore.worker_cancel` accepts a
   worker-audience credential of kind `worker` and only when its principal is the dispatcher
   worker of the route's admitted operation. It is the same reduce-only stop as a human
   `cancel` (desired `stop`, grant cancellation requested, no resume) and nothing more: it
   cannot pause, approve, start or touch another worker's route. It needs the current route
   revision and proposal digest, is idempotent by key, and a stale pair is a conflict.
2. **Renewal and issuance are the existing ones.** The executor enrollment already issues
   the worker credential and `executor_enrollment renew` renews it, so no credential kind,
   enrollment step or human issuance is added.
3. **Audit.** The route state records `stop_provenance` with `credential_kind: worker`,
   `decision_class: other_credential`, `cancel_authority: executor_worker` and the worker
   principal id; the grant cancellation event is written as before. The live-check receipt
   records `cancel_authority: executor_worker` and `cancel_recorded` (whether the cancel
   was accepted).
4. **Config.** `cancel_credential_file` is removed from `tessera.salad.live-config.v1` and
   `tessera.salad.campaign-executor.v1`; both stay closed, so a stale config naming it is
   refused with the usual closed-config error and must drop the field. `salad_live_setup`
   no longer issues a cancel credential or writes `cancel-token`.

## Consequences

- An operator setup needs no `human_session` token. A leaked executor credential could
  already dispatch its own routes; it now also stops them, which only reduces spend.
- Revoking an executor enrollment removes its routes' only non-human cancel path; revoke
  only after they end.
- Human `cancel`, `pause` and every approval path are unchanged and still need a human.
- Not done: a separate cancel-only credential kind. The worker's own-route scope already
  gives least privilege without a migration.
