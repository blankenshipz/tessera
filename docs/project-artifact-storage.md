# Project artifact storage

Schema v26 adds retention-aware experiment evidence to private local project
persistence after artifact quarantine. The
worker transfers declared output only to quarantine first. Control verifies the
retained BLOB checksum/size, records durable intent, then asks the narrow backend
to stage, commit, and reconcile an object keyed by tenant/project/artifact identity. Storage records
only backend kind, deterministic namespace/object metadata, retention class, and
timestamps; they contain no raw credentials, URLs, or worker paths.

`filesystem-local-v1` uses private hashed tenant/project directories, regular
single-link files, no-follow checks, temporary publication, and conditional link
creation. Crash recovery removes only a matching stale stage hard link before
rechecking the published object. Replays with identical bytes reconcile; a conflicting identity,
truncation, link, root replacement, or checksum mismatch fails closed. The fake
backend is injectable only for deterministic fault tests.

Historical SQLite BLOB artifacts have no placement and remain legacy by
absence of a binding/placement. New development-worker manifests and checkpoints
require committed, backend-reverified matching placement once the project binding
exists; checkpoint and human bookkeeping recheck it too. An explicit empty
declared-output placement records a committed zero-output operation without
inventing an object. Placement
does not classify/release bytes, accept a finding/result, change a cost, or
establish provider/scientific success.

Attempt-artifact and measurement-artifact evidence links use one transactional
admission path. A placed object must have active retention metadata; tombstoned,
delete-intent, degraded, and deleted objects cannot receive a new evidence link.
Those durable evidence references block later tombstone/delete admission in the
same tenant/project. Existing experiment links are indexed by the v26 migration
without changing their evidence or storage state. For compatibility, an unplaced
legacy SQLite artifact may still be linked as evidence: exports label it
`legacy_unplaced`, not project-retained. Exports label placed evidence with its
actual retention state, including `degraded` or `deleted`; artifact quarantine
remains an intake classification and is not a custody claim.

The default persistence boundary is the project store. Quarantine remains intake;
external export/move is a separately authorized future policy operation. There is
no S3 SDK, cloud credential, public URL, or production/pilot activation.
Backups fail closed if they would omit committed project objects.

Schema v46 can read a committed `active` local object only through a live,
human, project-scoped custody release for the exact quarantined manifest that
contains it. Retrieval rereads and rehashes the private object; an unavailable,
replaced, deleted, degraded, or mismatched object is not previewed or exported.
The same boundary supplies private reproduction bundles with an explicit byte
cap and missing-material declarations. It does not create an object URL, public
download, input release, external export, or a change in artifact classification.

This storage boundary is independent of planned Kubernetes Secret delivery and
control-plane provisioning. A Secret is not an artifact store or project object,
and no automatic destructive teardown/deletion follows target detachment.
