# 0005: Explicit development native CPU backend profile

Status: accepted for Stage 1 development evidence only.

## Decision

`development-local-cpu-v1` is an explicitly selected native worker backend
around the existing `local-cpu-v1` frozen control package. It is not a control
plane deployment mode, scheduler target, persistent-storage capability, or
pilot profile. A native worker must be given an already human-issued grant and
a private project-scoped worker credential file; it first heartbeats a prior
owner registration, then acquires the lease. It cannot register itself, issue
or reconcile grants, approve plans/releases, commit result checkpoints, or
claim provider/scientific success.

The worker validates the package's `local-cpu-v1` identity, its development
output marker/root and its local allowlisted pinned Docker image before start.
It persists each lease-renewal idempotency key before sending it and replays
that key after a lost response. Existing supervisor intent/journal semantics
remain the start/restart fence.

On a fresh control-acknowledged confirmed exit it seals only declared output
paths and transfers them through the existing artifact manifest protocol.
Receipts must remain `quarantined`/`unclassified`; only then may the marked
development workspace be reclaimed. Disconnect, revoked authority, output
failure, lost transfer response, or cleanup uncertainty retains the workspace
and reports no success.

## Consequences and remaining evidence

The profile is opt-in through `tessera-worker development-run`; there is no
Compose worker, Docker socket mount in control, ambient token argument, or
global runtime configuration. Its Docker fixture is synthetic and local.

This does not satisfy the Linux hard byte/inode quota, Docker-host restart, or
Minikube evidence required for a persistent/pilot backend. It also does not
release artifacts, settle costs, reconcile grants, accept results, or activate
unattended research dispatch.
