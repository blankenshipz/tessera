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

## Goal-to-decision attempt provenance

Schema v28 requires each new experiment attempt to name exactly one typed
provenance source. An `admitted_operation` attempt is bound to its immutable
variant/design and that variant's recorded task-plan digest. The operation must
belong to the same tenant/project, match that task-plan basis, retain an admitted
package digest, and have a matching attributed `exited` worker observation.
`unknown`, `running`, missing, and plan-unrelated operations cannot be recorded
as terminal experiment attempts. An operation can bind only one such variant.

The attempt preserves the operation's desired cancellation state separately from
its observed execution state and whether termination was confirmed. Its outcome
is still a human-recorded research label; neither an exited operation nor a
cancel request establishes scientific success. Non-computational work uses the
explicit `human_reported_observation` source, with no operation ID and a required
human observation. It may link project artifacts as human-reported evidence, not
as execution proof. v27 rows migrate as `legacy_operation_unverified`: no plan,
package, terminal observation, or scientific result is inferred from them.

## Fake-provider receipt slice

Schema v19 adds an empty-by-default, separate one-to-one receipt record. The
only admitted provider kind is the deterministic injected `fake` implementation;
there is no cloud, model, credential, paid-provider, or fallback path. Its
version-pinned `local-provider-v1` adapter only accepts an injected literal-loopback
HTTP fixture. It disables proxies and redirects, accepts bounded JSON requests and
responses under a bounded deadline, and exposes only submit, lookup, cancel, and
accounting-observation routes. The fixture token is test-only transport input, not
a provider credential.

The local worker seam persists a submit intent after the supervisor's start intent
and authority recheck. It derives one stable provider-submit idempotency key from
the operation ID and keeps the operation lookup key unchanged. A lost submit reply
is uncertain and recovery observes that lookup key rather than submitting again.
Lost lookup or cancel replies likewise leave uncertainty until a later lookup; a
cancel reply is never treated as termination without a fresh exited observation.
Receipt payload identity is bound to its operation, lookup, idempotency and provider
IDs; receipt/report retries reuse the stored payload and idempotency identity.
Provider IDs and receipt identity conflict rather than overwrite. Accounting observation
can only report a bounded synthetic usage value or `unknown`; `unknown` is not
zero, settlement, or permission to release the reservation. Unknown, stale
authority, and uncertain cancellation remain visible. The receipt is bounded
operational evidence only, not proof of Docker execution, cost/accounting,
artifacts, results, or scientific success.

If a receipt/observation report, heartbeat, or renewal cannot be confirmed, the
development worker keeps its durable intent and output reservation unresolved
and attempts cancellation without waiting for that control-plane report. Local
adapter termination and fake-provider cancellation are independent attempts;
failure to confirm either remains unknown and is never reclaimed or reported as
provider termination. Restart reads a fresh, durably keyed package response so
an already-running reservation can retain a renewed lease without resubmission.

## Immutable human outcome and revocation reconciliation

Schema v33 stores each human resolution of an admitted experiment attempt in a
separate immutable row. The legacy outcome/state fields remain a compatibility
projection, while worker and fake-provider reports update separate operational
state/observation fields. Thus a late `running`, `unknown`, or `exited` report can
be retained without replacing a human `unknown` or other outcome label.

Execution `succeeded` and `cancelled` resolutions require the original bound
package and operation plus an attributed, confirmed exited observation. A cancelled
resolution additionally requires the durable cancellation request; an unconfirmed
exit is not success/cancellation evidence and may be resolved as `unknown` with a
reason. A package refresh does not create restart history; callers must carry the
explicit restart intent.

After revocation, the control plane rejects starts, renewals, replacement packages,
and ordinary observations/receipt updates. It accepts only a matching original
operation/worker/lease confirmed-exit report, so termination can be reconciled
without reopening execution or result authority.

Credential-access leases are not provider receipts or operation inputs. If a later
adapter needs to associate one with an operation, it may retain only the opaque
credential-access lease ID; neither its secret reference nor its value belongs in
operation metadata, receipt payloads, journals, events, or diagnostics.

The approved future delivery boundary is pluggable: a local environment source and
Kubernetes Secret materialization are distinct delivery implementations, not
provider receipts. Each requires explicit human authorization tied to the frozen
plan, target, namespace where applicable, task, lease, and epoch. No journal entry,
skill request, or agent output authorizes delivery or permits raw `kubectl`/cloud
credentials.
