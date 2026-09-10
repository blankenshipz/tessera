# 0008: Project artifact storage is durable custody, not release

Status: accepted for local schema-v26 development storage.

Worker output slots are temporary execution-plane custody. After quarantine
checksum/size validation, development-worker output is staged, conditionally
committed, and reconciled into a private project artifact backend. The initial
backend uses deterministic hashed tenant/project namespaces, atomic no-overwrite
publication, and no worker-supplied filesystem path, URL, or credential.

SQLite records an intent before any backend call and commits placement authority
only after backend reconciliation. A failed process can replay an intent; the
filesystem backend repairs a stale temporary hard link only when it names the
published object's exact inode. Manifests, checkpoints, and bookkeeping acceptance
read/checksum the backend object again.

SQLite BLOB artifacts created before v22 remain explicitly legacy and receive no
invented placement. A binding makes new manifests/checkpoints require committed,
integrity-matching placement. Placement retains unclassified quarantined bytes:
it does not release data, accept findings/results, settle costs, or claim
provider/scientific success. Cleanup requires the committed placement receipt.

The default is project persistence; quarantine is intake. Schema v24 adds explicit
project-default, held, and short-lived retention metadata with activity timestamps.
Deletion remains a manual tombstone followed by durable deletion intent and exact
backend identity verification. Referenced, checkpointed, accepted, and held objects
refuse deletion. Missing, corrupt, or uncertain delete outcomes remain degraded
incidents, never a successful cleanup. Backup currently fails closed when committed
external objects exist rather than producing an incomplete bundle.

Schema v26 makes goal-to-decision attempt-artifact and measurement-artifact links
durable retention references. New links to placed objects require active retention,
and the delete check reads those tenant/project-scoped references. SQLite-only
legacy artifacts remain linkable without an invented placement; exports identify
them as `legacy_unplaced` rather than retained project objects.

S3/IAM/KMS, public URLs, external movement/export/release, production activation,
and a pilot storage acceptance are not implemented. ADR 0003's worker-slot gate
is unchanged.
