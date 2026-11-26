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

## Amendment: when the live check cancels, and a cancel that is not recorded

- The live check does not cancel a route whose last post-start observe read `running` for a
  control stall (a control database write lock, a bounded local call that ran out) or a
  transient read failure; it retries at the next interval inside the run deadline and poll
  bound. The guardian keeps its own deadline, budget and heartbeat-silence stops. A lease
  renewal that stalls does not hold back the guardian heartbeat, which re-runs use-time
  authority.
- Every cancel records a closed `cancel_cause` on the receipt; a cancel of a running route
  for any reason other than completion, the guardian's recorded stop cause, the deadline or
  the budget has the reason `running_route_cancelled_<class>`.
- When `worker_cancel` is not recorded, the guardian cancel is still sent, the receipt
  records `cancel_recorded: false`, `cancel_failure` and the reason `cancel_unrecorded`, and
  the run never passes. The executor holds the outcome, for a bounded time, until the
  guardian's confirmed stop of the exact group is reported (ADR 0160).

## Amendment: a control refusal on a running route

- While the last post-start observe read `running`, a `Forbidden` (or an HTTP 401/403)
  cancels the route only when it is a verified revocation: control's readiness probe answers
  ready and the refusal's detail is exactly one of control's authority-gone details
  (`AUTHORITY_GONE_DETAILS` in `tessera_worker/control_channel.py`), never a substring. Any
  other `Forbidden` (a generic `Unauthorized`, an empty or unreadable detail, `Authenticated
  exact Salad route required.`), or any `Forbidden` while control is unready, takes the
  control-stall path above. A refused keepalive keeps the beat from the guardian, so it is
  still tolerated only inside the guardian silence bound. Deadline, budget, a store
  `Conflict` and the guardian's recorded stop cause cancel as before; before `running`
  nothing changes.
- Every refusal leaves bounded evidence: `cancel_cause.refusal` and the receipt's
  `control_refusal` name the driver step, the in-process control call, the HTTP status when
  there was one, control's plain-text detail (anything URL-, path- or token-shaped, or over
  160 characters, is replaced by `unrecognized_detail`), whether it was verified, control's
  readiness, and whether it was skipped. `cancel_cause.class` stays closed.
- Control reads a credential and its latest renewal in one read snapshot, so a renewal
  committed between the two reads can no longer be read half-applied and refused as
  `Unauthorized`; a lock stays an `OperationalError`. A timed-out licence-label read stays a
  `TimeoutError`, not a `Forbidden`.

## Amendment: the completion stop is not an execution cancellation

- The live check's own cancel after a verified pass fences the grant and its operation
  (`cancel_requested`) like any cancel. `resolve_attempt` read that fence as a request to
  abandon the run and refused the success ("Execution success cannot override a requested
  cancellation."), so the attempt failed `step_refused` and no output was indexed.
- Control now records an execution success over a `cancel_requested` operation only when its
  own records show the fence is exactly that completion stop: the campaign Salad attempt's
  stored live receipt is `passed` / `verified_journey` with `stop_confirmed`,
  `cancel_recorded` and `cancel_cause.class` exactly `execution_complete`; the exit is
  confirmed; the route's stop provenance is the route's own dispatcher worker
  (`cancel_authority: executor_worker`), the provider stop is confirmed and the route was
  never reallocated; the grant's one cancel transition was that worker's; and the manifest is
  the operation's own, lists exactly the receipt's outputs, is complete, custody-released at
  its digest and retained, with every file's custody row at the listed sha256 and size. Any
  other cancel (a person, an operator grant revoke, a campaign cancel, the deadline, the
  budget, a guardian refusal, an error) or any missing piece still refuses. No receipt status
  is added.
- Attempts an earlier build failed this way are recovered by `POST
  /campaigns/{id}/reconcile-completion-cancels` (`tessera campaign
  reconcile-completion-cancels`; `GET /campaigns/{id}/completion-cancels` is the dry run),
  for the owner's control credential only and only on a finished campaign. It requires the
  exact refusal on a `failed_permanent` / `step_refused` attempt with Salad verdict `ok` and no
  output rows, the same evidence as above, and a re-hash of every output's bytes in custody
  (checked again, by chunk list, in the write transaction). In one transaction it resolves the
  admitted attempt `succeeded` through the same evidence rule, writes the stage's output rows,
  moves the attempt and stage to `succeeded` (the campaign too, with its stop reason cleared,
  once every stage has succeeded) and appends a `campaign.completion_cancel_recovered` event
  sealed by its own digest and bound to the terminal receipt's digest, listing before and
  after states, the evidence and each output's name and sha256. Charges, holds and
  settlements are left as they are. A second run is a no-op success.
- The terminal receipt stays immutable. `resolve_artifact` accepts an output that receipt does
  not list only when a sealed recovery event bound to exactly that receipt's digest lists the
  same stage, attempt, output name and sha256; every other check is unchanged. The
  human-only `reconcile-unclaimed-salad` hold is untouched.
