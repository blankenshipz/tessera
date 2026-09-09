# 0006: Durable external-operation journal

Status: accepted for the development-local CPU profile only.

## Decision

Schema v18 records one `external_operations` intent in the same control-plane
transaction that returns a `local-cpu-v1` executable package. The intent binds
the organization, project, task, grant, lease, worker, epoch, deterministic
operation ID, package/request digests, Docker lookup key, adapter/profile
versions, `manual_reconciliation` retry class, and separate desired/observed
states. The event and idempotency response commit before a worker can call
Docker.

The lease has one operation identity. A repeated request returns that operation
and package; a changed package or identity conflicts rather than creating a
second submission identity. First attributed provider ID is immutable. Grant
revocation transactionally changes an extant operation's desired state to
`cancel_requested`; it does not call Docker.

## Compatibility and limits

The migration creates no rows for v17 leases or observations and leaves their
operation column null. Compatibility observations are accepted only for a lease
with no operation journal; an operation-journaled lease requires its exact ID,
worker, epoch and provider binding. Existing artifacts, manifests, and synthetic
checkpoints retain their prior derived-operation compatibility boundary rather
than being retroactively bound to invented v18 operations.

This journal is not provider dispatch, a credential path, a provider receipt,
artifact release, result acceptance, cost settlement, or scientific success.
