# Ordinary worker renewal: run-5 diagnosis and normal acceptance

## Observed boundary (2026-10-04)

The sanitized `installed-maintenance-run5.json` receipt establishes a real
completed Job, 36 retained bytes, value 385, late quarantined output, and a human
`cancelled` resolution. It does **not** contain the lease cancellation reason,
renewal timestamps, worker action outcomes, or the relative time of grant
revocation. Thus it cannot distinguish a deliberate negative-stage fence from
lease expiry, outage fencing, credential loss, or delayed collection. Do not
attribute that result to missing worker renewal or relabel it as normal success.

The source already implements ordinary, desktop-independent renewal:

- `kubernetes_remote_service.main` constructs `KubernetesDispatchWorkerService`
  and runs it continuously when `--max-cycles` is omitted.
- `KubernetesDispatchWorkerService` defaults to ten-second execution renewal,
  thirty-second registry heartbeats, and a bounded control-outage deadline.
- `KubernetesClaimIntentJournal.action` commits the renewal payload, sequence and
  stable idempotency key before calling **POST `/worker/leases/renew`** with
  `lease_id` and `expected_epoch`. The route, not the internal store mutation
  path `/execution-leases/{lease_id}/renew`, is the worker HTTP interface.
- Renewal completion atomically stores the current lease deadline and marks the
  same durable action complete. Lost replies/restarts retain and replay the
  original key; they never create a replacement key to evade an unresolved call.
  Replay must still pass the server's current-authority guard. Registry heartbeat
  alone is not lease authority.
- Control permits renewal only for the assigned live worker and exact epoch,
  current approved task/grant, ready registry, live credential/agent authority,
  and authorized releases. The extension is at most thirty seconds and cannot
  cross immutable `hard_expires_at` or revive an expired lease.
- `runtime_packages._package` seals `hard_expires_at`, not the renewable
  thirty-second expiry, into the approval-bound package. The live lease is a
  separate response envelope. No package digest rewrite or backend package
  refresh change is needed for this existing running-Job renewal path.
- The supervisor retains its hard deadline across restart and cancels the exact
  owned Job on expiry, revocation/authority loss or outage bounds. An acknowledged
  command is not termination proof; uncertain cancellation remains owned and
  requires reconciliation.

No second keeper, new runtime code, backend edit, authentication-file access,
service restart, Docker action or deployment was introduced for this diagnosis.
The existing authenticated lease-renewal command is worker-owned; a harness or
human must not call it as a substitute for ordinary service supervision.

## Normal installed sequence for the parent

1. Preserve run-5 receipts/journals. Use separately authorized fresh task/attempt
   bindings and still-live worker/agent credentials; do not revive expired ones.
2. Confirm the installed worker image contains the existing supervisor and its
   normal invocation does **not** set a bounded `--max-cycles`. Use the existing
   supervised service, private runtime references and same owned workspace.
   The source entry point is `python -m tessera_worker.kubernetes_remote_service`
   with the explicit `--target-file`, `--ca-file`, `--kubernetes-token-file`,
   `--credential-file`, `--control-ca-file`, `--control-url` and `--workspace`.
   Do not start a second process over the same workspace.
3. Prepare target/network/input pins before acquiring the short execution lease.
   Then acquire lease -> freeze its epoch in the final bindings -> admit -> issue
   the immutable package -> dispatch. Complete this setup while that initial
   lease is current. Running-job renewal cannot revive a lease lost during setup.
4. Keep the normal grant/task/credential active while the ordinary worker claims,
   executes, reports terminal evidence, collects and uploads declared outputs.
   Do not perform expiry waits, deliberate control downtime, grant revocation,
   or cancellation against this normal attempt before its outcome is committed.
5. Assert actual normal control evidence, not just Kubernetes completion: exact
   operation/Job/Pod UIDs, terminal observation, declared retained manifest and
   digest, `late=false`, valid human custody, and successful supported attempt
   resolution. Quarantine itself remains expected custody policy, not acceptance
   or scientific validation. If collection is late, preserve it and diagnose;
   do not force a successful resolution or replace bytes.
6. Exercise expiry/revocation/cancellation on **separate bounded grants/attempts**
   after the normal outcome is retained. For a restart-continuity test on active
   work, keep real downtime inside the existing lease/outage/hard bounds. Do not
   add a private renewal loop to mask a service outage.

## Exact missing evidence before any lifecycle change

Return a sanitized, operation-correlated timeline for run 5:

- execution lease issued/renewed/expires/hard-expiry times, fixed epoch, and the
  actual `execution.grant_cancel_requested` event reason, actor role and timestamp;
- worker durable `dispatch_actions` renewal sequences, stable-key fingerprints,
  completion bits, `dispatch_supervision` lease/outage/last-wall bounds, and the
  worker cancellation proof **reason** (no credentials or full response bodies);
- ordinary worker start/restart/stop generation and configuration, terminal
  Job/Pod observation/report times, collection/upload/manifest times;
- negative harness `human_revoke`, expiry-wait and external-restart times, with
  their target grant/attempt identities.

This distinguishes a negative run from an execution-renewal problem or a
collection-phase delay. Source collection is synchronous after terminal report;
the running-Job renewal regression does not prove renewal during that phase.
Investigate that narrower seam only if the correlated timeline identifies it.
The existing public API does not advertise a worker GET-current-lease endpoint;
any new query-based recovery contract would require separately owned backend
integration rather than a worker-side authority guess.

## Focused verification

Existing synthetic cluster/control-clock fault tests were selected in
`tests/test_kubernetes_dispatch.py` with:

```text
renewal or renewals or revocation or immutable_hard_deadline or
current_lease_expiry or control_hard_expiry or delayed_heartbeat or
full_control_outage or persisted_outage or wall_clock_rollback or
supervisor_cancels_long_running
```

Pytest reported **24 passed, 38 deselected, 2 dependency deprecation warnings in
115.45 seconds**. The outer shell tool reached its 120-second timeout after the
pytest summary; no shell exit code was observed. These tests use temporary SQLite,
real in-process API handlers, fake Kubernetes effects and controlled clocks;
they are not a fresh installed-service or live Job run. Passing coverage includes
renewal beyond the initial lease, exact schedule, lost-response replay/restart,
atomic receipt commit, UID/epoch/expiry response fences, independent outage bounds,
revocation, immutable hard limit and clock rollback. No desktop rechecks ran.
