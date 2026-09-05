# Authorized worker input delivery

`POST /worker/leases/{lease_id}/inputs` accepts `{"expected_epoch": 1}` with a
worker-audience bearer credential and `Idempotency-Key`. The assigned worker needs
a current lease, active grant, unchanged approved plan and active public-input
release. The service checks issuer/worker eligibility, project scope, authority
epoch, expiry and retained input integrity in the same transaction as the audit
event and replay receipt. It checks lease expiry again after assembling bytes.

The response contains `receipt` and `snapshot`. The snapshot includes its ID,
pinned digest, original manifest (including source-root metadata and exclusions),
and files with `relative_path` and `content_base64`. Existing bounds limit inputs
to 16 MiB and 1,000 files, with a 1 MiB manifest; base64 increases transport size.
Original provenance/classification labels remain unchanged. The separate human
public release authorizes this destination; it does not prove source accuracy.

The receipt records authorized intent, with `delivery_confirmed: false`. The
event and idempotency record contain metadata only. Retrying rebuilds bytes after
fresh authority and integrity checks, reusing the original receipt without a
duplicate event. Revocation blocks subsequent requests, including cached-key
replays; it cannot retract bytes already sent or prevent delivery of a response
authorized immediately before revocation.

`tessera_worker.inputs.materialize_snapshot` verifies the expected snapshot digest,
lease ID, epoch and worker ID supplied independently by its trusted caller. It
checks canonical manifest integrity, exact file membership, sizes and SHA-256
checksums before creating a fresh private directory. Absolute/traversal paths,
duplicates, file/directory collisions, case aliases and Unicode normalization
aliases are rejected. It does not extract archives or execute files. Files are
read-only (`0444`) and have no executable permission; snapshot directories are
traversable (`0555`) under a private supervisor parent (`0700`).
The trusted owner can still change these permissions, so they are not a sandbox.

The caller must use an authenticated control channel and a private POSIX staging
parent under supervisor custody. Failed staging removes its new tree and preserves
existing files. No retained source root is resolved on the worker and no existing
directory is merged or overwritten. Downloaded bytes are not execution authority.

Synthetic tests cover HTTP delivery through local materialization, replay,
revocation, expiry, corruption, wrong audiences/workers, stale epochs and audit
rollback. [CPU runtime integration](cpu-runtime.md) connects these components to
the supervisor and verified read-only mounts. Live container execution, pinned
skill loading, receipt acknowledgement, artifact collection,
retention/cleanup scheduling and end-to-end research execution remain incomplete.
