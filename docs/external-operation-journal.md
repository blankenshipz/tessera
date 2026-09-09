# External-operation journal

Schema v18 adds a durable intent/lifecycle row before the control plane returns
an executable development-local CPU package. It records the tenant and complete
authority binding, deterministic operation ID, package digest, adapter/profile,
stable `tessera-op-{operation_id}` Docker lookup key, request digest,
`manual_reconciliation` retry class, desired state, observed state, and optional
immutable provider ID.

`POST /worker/leases/{lease_id}/package` is idempotent. A lost response can be
retried with its key and returns the same operation/package. A different request
that changes the frozen package conflicts; it cannot create another operation.
The worker validates the returned journal metadata before Docker interaction and
includes `operation_id` in observations. Observations for a journaled lease must
match the operation, tenant-derived worker, lease and epoch. Unknown observations
remain unknown; a differing provider ID is rejected without overwriting the row.

Grant revocation writes `cancel_requested` to the operation desired state in the
same transaction as grant cancellation. It makes no provider call. On foreground
tick exhaustion the development worker requests cancellation, reports the result,
and retains its workspace as unresolved. Recovered terminal supervisor journals
perform a fresh read-only Docker observation where possible and never restart.

Migration deliberately does not backfill v17 leases, observations, artifact
records, manifests, or synthetic checkpoints. Those legacy records retain null
operation journal attribution or their existing derived deterministic identity.
They remain quarantine-only and are not new execution receipts.

## Fake-provider receipt slice

Schema v19 adds an empty-by-default, separate one-to-one receipt record. The
only admitted provider kind is the deterministic injected `fake` implementation;
there is no cloud, model, credential, or paid-provider path. Its local worker
seam persists a submit intent after the supervisor's start intent and authority
recheck. A lost submit reply is uncertain and recovery observes the stable
lookup key rather than submitting again. Receipt/report retries reuse the stored
payload and idempotency identity. Provider IDs and receipt identity conflict
rather than overwrite. Unknown, stale authority, and uncertain cancellation
remain visible. The receipt is bounded operational evidence only, not proof of
Docker execution, cost/accounting, artifacts, results, or scientific success.
