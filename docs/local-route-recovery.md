# Abandoned local route: isolated custody preparation

The user journey is: existing admitted control service → guarded route repair →
research MCP status/list/detail → reconnect and read the same immutable proposal.
Exit does not stop the independent control plane. This module implements only
preparation, not the route repair or the remaining live acceptance.

`tessera_operator.route_recovery` is deliberately not imported by any running
entrypoint, registered as MCP, or exposed as a CLI. No production paths or OS
observers are supplied. `prepare_custody(directory, expected_intent_sha256,
expected_identity, inspect_identity=..., inspect_processes=...)` is an internal
adapter interface, **not** a tool an agent can invoke against live receipts.

It acquires the existing supervisor lock, checks a private exact incomplete
intent, validates its byte digest and admitted Pod/image/PVC identity through a
trusted callback, and refuses incomplete process inspection or any process or
listener. Its only publication is a private, fsynced custody directory containing
the original bytes and a deterministic plan marked
`custody_only_integration_required`. The original route receipt is untouched.
Identical preparation rechecks prerequisites and custody without rewriting it.
Partial preparation is retained and blocks later preparation; it is not cleaned
up or silently resumed. Failures after rename can leave a complete bundle; reuse
verifies its contents but does not claim that the original fsync succeeded.

## Why preparation cannot clear the intent

The current launcher writes intent before spawn and records process identity only
after readiness. Even a startup failure that terminates the child retains the
intent. The supervisor drops the error body, so the original trigger is unknown.
A process snapshot showing no kubectl is not proof against a concurrent launch.
The supervisor lock serializes ticks, but other calls to the current route
function do not acquire it. This module cannot establish that missing invariant.
`publish_reconciliation` therefore always refuses, including caller-supplied
claims that writers are fenced. No process is adopted, killed or spawned.

## Required integration before any live recovery

1. Make **all** route launch/reuse/reconciliation paths share one verified lock;
   define non-reentrant ownership so a supervisor tick does not deadlock its child
   stdio server. Close inherited descriptors, and reject unknown launch writers.
2. Add a trusted bounded inspector for admitted generation, namespace, Deployment,
   image, PVC/PV and sole Ready Pod; process enumeration must be complete, scoped
   to every potential route child, and fail closed on permission/parse errors.
   No environment, credentials or arbitrary command strings enter receipts.
3. Under that lock, revalidate expected digest, admitted identity and fresh OS
   state. Publish an immutable retirement decision bound to the original bytes,
   using an atomic no-overwrite commit record. The ordinary launcher must consume
   that decision under the same lock rather than requiring deletion of evidence.
   Define crash states before and after each write; unknown states refuse.
4. Add allowlisted phase/exit-category receipts at spawn, readiness, identity and
   commit boundaries. Never copy raw stderr or exception strings into diagnostics.
5. Expose the narrow recovery tool only after a separate bounded authorization
   assessment and native permission. Preparation permission does not authorize
   retirement or launching. Then call the existing guarded launch separately and
   verify actual MCP status, expiry/scopes, proposal revision/digest and reconnect.

Synthetic tests exercise custody, drift, lock contention, rejected observations,
crash windows and idempotency in temporary directories only. They prove neither
OS inspection completeness nor live process exclusion, route health or readback.
No active image, frozen candidate, credentials, runtime database or live receipt
is modified by this implementation. No new connection or capability is exposed.
