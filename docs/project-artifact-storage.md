# Project artifact storage

Schema v22 adds private local project persistence after artifact quarantine. The
worker transfers declared output only to quarantine first. Control verifies the
retained BLOB checksum/size, then asks the narrow backend to stage, commit, and
reconcile an object keyed by tenant/project/artifact identity. Storage records
only backend kind, deterministic namespace/object metadata, retention class, and
timestamps; they contain no raw credentials, URLs, or worker paths.

`filesystem-local-v1` uses private hashed tenant/project directories, regular
single-link files, no-follow checks, temporary publication, and conditional link
creation. Replays with identical bytes reconcile; a conflicting identity,
truncation, link, root replacement, or checksum mismatch fails closed. The fake
backend is injectable only for deterministic fault tests.

Historical SQLite BLOB artifacts have no v22 placement and remain legacy by
absence of a binding/placement. New development-worker manifests and checkpoints
require committed matching placement once the project binding exists. Placement
does not classify/release bytes, accept a finding/result, change a cost, or
establish provider/scientific success.

The default persistence boundary is the project store. Quarantine remains intake;
external export/move is a separately authorized future policy operation. There is
no S3 SDK, cloud credential, public URL, deletion, or production/pilot activation.
Backups fail closed if they would omit committed project objects.
