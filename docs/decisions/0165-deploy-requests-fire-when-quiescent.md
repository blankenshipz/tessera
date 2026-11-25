# 0165 — Deploy requests fire when quiescent

## Status

Implemented. No schema change (records and receipts are files). Builds on
[ADR 0099](0099-deploy-upgrade.md) (the checked upgrade) and [ADR 0098](0098-deploy-check.md) (the
preflight). It adds no authority: it runs the same `tessera deploy-upgrade --execute` with the
plan file the requester named, at a later moment, and nothing the operator could not run by hand.

## Context

A release can be merged and gated and still not deployable: restarting the control plane or
executor while a campaign stage attempt is running on a remote route risks racing that route. The
deploy then waited for a person to notice that the route finished and to run the upgrade, with no
record of what was asked for, who asked, or why it had not happened yet.

## Decision

1. **A request is a record.** `tessera deploy-request create` writes one
   `tessera.deploy-request.v1` file under `<state-dir>/deploy-requests` holding the request id
   (`dr-` and 32 hex), the release (a full 40-hex revision), the requester label, the sha256 of the
   gate evidence that cleared the release, the operator plan path and its sha256, the main ref
   (default `main`), creation and expiry times (default 24 hours, at most 168), the status, the
   reason, the current blockers, a check count and the fire result. The gate evidence is given as
   a file (hashed, and re-hashed at fire time) or as a digest. Creating the same release, plan and
   evidence again returns the open request; a different one is refused while a request is open.
2. **Tick.** `tessera deploy-request tick` evaluates the one open request once. Idle when none is
   open. Otherwise, in order: a request found `firing` becomes `failed` (`fire_interrupted`); an
   expired one is `expired`; the plan file must hash as it did at creation (`plan_changed`),
   still load (`plan_invalid`) and still name this release and state directory
   (`release_mismatch`); the gate evidence file, when one was given, must hash as before
   (`gate_evidence_changed`); the head of the main ref in the plan's `repo_root` must still be the
   release (`main_moved`). Those are refusals and end the request. What remains are blockers,
   which only make the request wait: the blocker probes, a main head git cannot read, and the
   preflight of the checked upgrade. With no blocker the request is marked `firing`, the mark is
   written, and only then is the upgrade run.
3. **Invariants.**
   - Single flight: every change happens under an exclusive, non-blocking lock on the request
     directory held for the whole evaluation and the upgrade. A second tick, create or cancel
     while one runs reports busy and changes nothing.
   - At most once: the `firing` mark is on disk before the upgrade starts, and a `firing` record
     found later is never fired again.
   - Only the requested release: the plan, its `source_revision`, the gate evidence and the main
     head are all re-checked at fire time. A main that moved without new gate evidence is a
     refusal; the remedy is a new request.
   - Fail closed: an unreadable database, an unreadable main head, a failing probe or a failing
     preflight is a blocker, never a pass; a changed or unreadable input is a refusal.
   - Receipts: a request, a change in the blocker set (not every tick), the fire, its result, a
     refusal and a cancellation each write an owner-only receipt carrying a sequence number and the
     digest of the previous receipt, so a missing or reordered receipt shows.
4. **The upgrade is called as is.** The restart internals of ADR 0099 (stop order, backup, migrate,
   start, health, smoke, its own receipt) are unchanged; the request records the result, the failed
   step and the digest and name of the deploy receipt.
5. **Blockers are pluggable probes.** A probe takes the state directory and returns blockers. The
   built-in probe is provider-neutral: it counts campaign stage attempts in `granted`, `admitted`,
   `running`, `exited`, `collected` or `unknown` per provider and status
   (`route.in_flight:<provider>:<status>`), read-only from the control database. It names no
   provider. A separate future change may let a driver that is safe to restart stop blocking; this
   one does not.
6. **Read surfaces.** `GET /admin/deploy-requests` and the MCP tool `tessera_deploy_requests`
   return the newest requests and their receipts, read-only, to the audience that may read the
   deploy report. The plan and gate evidence paths are left out. Creating, ticking and cancelling
   exist only as commands on the operator host.

## Consequences

- Nothing runs the tick by itself. Run it from a timer or `watch` outside the services the upgrade
  restarts, or the tick is stopped by the restart it started.
- A window remains between the last probe and the upgrade stopping services: a route could start
  in between. The request narrows the gap from a person's reaction time to seconds; it does not
  close it.
- An in-flight attempt whose state is `unknown` blocks until someone resolves it or the request
  expires. That is intended: its compute may still be running.
- A pending request that waits past its expiry ends `expired`; deploying then needs a new request.
- The probe set is code, not configuration; adding a blocker is a code change with its own test.

## Alternatives considered

- **Notify a person when the route finishes.** Still manual, and leaves no record of the intent.
- **Fire the upgrade from inside the control plane.** The upgrade restarts the control plane, so
  the actor would stop itself mid-run.
- **Skip the main check and deploy whatever the plan says.** A later merge would be deployed
  without its own gate; the refusal keeps gate evidence tied to the revision.
- **A database table for requests.** Needs a migration and the control plane available; files
  keep the request readable while the services are stopped.
