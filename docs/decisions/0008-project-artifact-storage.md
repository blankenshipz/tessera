# 0008: Project artifact storage is durable custody, not release

Status: accepted for local schema-v22 development storage.

Worker output slots are temporary execution-plane custody. After quarantine
checksum/size validation, development-worker output is staged, conditionally
committed, and reconciled into a private project artifact backend. The initial
backend uses deterministic hashed tenant/project namespaces, atomic no-overwrite
publication, and no worker-supplied filesystem path, URL, or credential.

SQLite BLOB artifacts created before v22 remain explicitly legacy and receive no
invented placement. A binding makes new manifests/checkpoints require committed,
integrity-matching placement. Placement retains unclassified quarantined bytes:
it does not release data, accept findings/results, settle costs, or claim
provider/scientific success. Cleanup requires the committed placement receipt.

The default is project persistence; quarantine is intake. External export/move
is a separate policy-gated operation. Automatic deletion is unavailable and
referenced/held retention must refuse deletion. Backup currently fails closed
when committed external objects exist rather than producing an incomplete bundle.

S3/IAM/KMS, public URLs, external movement/export/release, production activation,
and a pilot storage acceptance are not implemented. ADR 0003's worker-slot gate
is unchanged.
