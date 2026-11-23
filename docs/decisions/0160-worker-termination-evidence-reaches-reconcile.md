# ADR 0160: definitive termination evidence reaches control after the worker stops driving

Status: Implemented (no schema change). Builds on ADR 0046 (not-started reconciliation),
ADR 0053 (resolution on a confirmed exit), ADR 0054 (observation outbox), ADR 0063 (exit code
with the confirmed exit) and ADR 0153 (stuck campaigns drive leased grants through the
evidence paths). Narrows the first limitation of ADR 0153.

## Context

`POST /execution-grants/{id}/reconcile` and the campaign's confirmed-exit resolution
(ADR 0053) settle a leased grant only on a current reconciliation that is `exited` with
termination confirmed and uncontradicted (`current_operation_reconciliation`). Only an
observation from the worker that holds or held the lease, a fake-provider receipt or a recovery
observer can produce one.

Two places saw the end of a run and never told control:

1. The development poller writes a local `result.json` when its one-shot service ends a grant,
   and never drives that grant again. A service that gave up without a confirmed exit (a lease
   renewal or observation report failure, an unconfirmed prepare or cancel) leaves control with
   a `running` or `unknown` attempt and an unresolved grant. The container then exits (a SIGKILL
   from the service's own cancel, exit 137) or is removed, and nobody reports it.
2. The Salad campaign executor reports an exit only from a live-check receipt on disk. When the
   route subprocess timed out or left no receipt, the guardian could still close the
   reservation on a fresh exact provider read of the group stopped
   (`provider_confirmed_exact_group_stopped`), but that verdict never reached control.

Every supported route then refused (`reconcile` "requires confirmed termination",
`reconcile-expired-undispatched`, `reconcile-unclaimed-salad`, recovery reconciliation, cancel),
and the grants held project concurrency slots until a person intervened.

## Decision

1. **A poller sweep reports what Docker definitively says** (`tessera_worker.termination_evidence`).
   Each poll cycle, for at most 8 grants that have `result.json` or `abandoned.json`, a local
   Docker runtime descriptor and no settled marker, the poller reads the descriptor (lease,
   epoch, operation and plan identity) and the local observation journal (the container ids
   this operation reported, and which of them control accepted), then runs `docker inspect`
   for the recorded container id and for the operation's container name. It reports through the
   ordinary `POST /worker/observations` route with the poller's own credential, so control's
   existing checks apply: the reporting worker is the grant's worker and the lease's holder,
   the epoch and operation binding match, the grant is unresolved, the provider identity never
   changes within the lease, and the observation is in order and inside the lease window.
   - A container with this operation's labels that has `exited` or `dead` (not running, not
     restarting, not paused) with an integer exit code is reported as a confirmed exit with that
     `exit_code` and its container id. No contract change: this is the observation the live
     service would have sent.
   - When Docker answers "no such container" for both the recorded id and the name, and that id
     is the only one the operation ever reported and control accepted it, the sweep reports a
     confirmed exit with `termination_evidence: "container_absent"` and the recorded id.
   - Anything else holds and nothing is posted: no recorded or no delivered identity
     (`container_identity_unrecorded`), two identities or a different id under the name
     (`container_identity_conflict`), labels of another operation (`container_identity_mismatch`),
     a running, created, restarting or paused container (`container_may_run`), Docker not
     answering (`docker_unavailable`), or an unreadable descriptor or journal. The reason is
     written to a private `termination-evidence.json` marker in the grant directory (ids, states
     and times only). Identity and descriptor reasons are final and not re-checked; the others
     are retried with exponential backoff (at most 15 minutes).
   - The request is written to a private intent file before it is posted and replayed with the
     same idempotency key until control answers. A permanent refusal drops the intent and the
     grant is looked at again after the backoff (for example once the lease has ended). An
     operation whose confirmed exit control already accepted is marked reported without a call.
   - The sweep never starts, kills or removes anything. It is on by default with the real
     service; an injected test service needs an explicit Docker runner, so tests never reach
     Docker. A remote provider's grant (`provider-dispatch.sqlite3`) is skipped.
2. **`container_absent` is matched by control.** `ReportExecutionObservation` gains
   `termination_evidence` (closed values `container_absent` and `provider_group_stopped`), valid
   only on an `exited`, termination-confirmed observation with a provider identity and no exit
   code. For `container_absent` the store also requires a journaled operation, that the reported
   id is the operation's recorded `provider_id` and was reported by an earlier observation of
   this same lease by this worker, and that every lease of the grant has ended (`expires_at` in
   the past: an ended lease cannot be renewed, so no authority remains under which a container
   could be created). Otherwise it refuses with 409 and writes nothing.
3. **The guardian's exact-group stop is delivered by the Salad executor.** For an `executing`
   attempt with no receipt on disk, the executor reads the guardian status of the route's
   reservation (`StoreControl.guardian_stop`). The status reply now carries the reservation's
   bound `provider_id` (the group; additive). The executor reports only when that group equals
   the group control recorded on the route (UUIDs compare in any spelling, anything else
   verbatim) and the status is `stopped` (exact terminal, custody sealed) or `reconciled` with
   reason `provider_confirmed_exact_group_stopped`. It reports, as the worker that holds the
   lease, a confirmed exit with `termination_evidence: "provider_group_stopped"` and the route's
   group id, at a time fixed once in a private `group-stop.json` marker so retries replay the
   same request; at most 5 failed reports per attempt per process. Any other status, an operator
   reconcile, another group, no recorded group or an unreadable guardian holds (logged as
   `group_stop_held` with the reason). No outcome, charge or import is recorded. The store
   requires a journaled operation and that the newest state of every route of the operation that
   names a group names exactly this one.
4. **Receipted.** The `execution.observation_reported` event is written in the same transaction
   as the observation, as before, and is always written when evidence is given; it carries
   `termination_evidence` when present (and `exit_code` as before), with
   `observation_proof: trusted_worker_assertion`.
5. **Settlement is unchanged.** Control then holds an `exited`, confirmed reconciliation. The
   existing paths act on it with their own guards and accounting: the ADR 0053 resolution (after
   its output grace) closes a free worker-grant attempt `failed_transient` and reconciles the
   grant at zero on a running campaign and, through ADR 0153 rule 3, on a blocked or paused one;
   an absent container has no exit code, so it is `exit_confirmed_unresolved`, and exit 137 is
   the infrastructure retry. The stage's retry policy applies. `POST
   /execution-grants/{id}/reconcile` now accepts the grant; a billable route (Salad) is settled
   only there, by a person entering the settlement, as before.

## Consequences

- The field case closes without an operator call: a grant whose container was killed or
  removed after the poller gave up is reported within a few poll cycles and settled by the next
  runner tick past the grace, freeing its project slot for the retry.
- A Salad grant whose route left no receipt becomes reconcilable as soon as the guardian has
  confirmed the exact group stopped.
- What still holds is reported locally with a reason instead of silently staying pinned, and an
  expired claimed grant with no evidence still gets ADR 0153's `campaign.slot_held`.

## Limitations

- Docker's answer is trusted for the daemon the poller talks to. A poller pointed at a different
  daemon than the one that ran the container would see it absent; the operator owns that
  configuration (the same daemon the live service used).
- A container whose identity control never accepted (every report lost, or it never started)
  is not settled by this ADR; the not-started evidence (ADR 0046) or a person covers it.
- Control does not read the guardian itself for `provider_group_stopped`; it trusts the lease
  holder's assertion, as for every worker observation, after matching the group to the route.
- Closing an attempt whose exit was observed but whose report never arrived, within a bounded
  window, is not decided here; this ADR only delivers the evidence that such a rule consumes.
