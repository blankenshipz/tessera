# Outcome 0 environment bootstrap (offline contract)

`tessera_provisioner.outcome0_bootstrap` is the opt-in, **provider-disabled**
contract for a reproducible molecular-distillation environment.  It accepts a
human-approved capability declaration, a digest-pinned image and lock, and an
existing execution lease/runtime-package identity.  The declaration must name
every Python package, system package, model/data artifact, SHA-256, size, and
approved HTTPS or `fixture://` source prefix.

The supplied implementation is deliberately an offline fake-effect adapter. It
creates a deterministic isolated-workspace identity, records stable effects for
each declared install/download, verifies only caller-supplied local fixture
bytes before recording a completed artifact effect, and records a manifest/log
digest. Declaration ordering is canonical and artifact URIs reject queries,
fragments, escapes, and traversal so an approved prefix cannot be widened. It does not invoke a shell, package
manager, container engine, network client, Salad, provider credential, or live
provider. Consequently it is not a live environment installer and is not a
default-CI/live-provider path.

The readiness projection is bound to the execution `lease_id`, `operation_id`,
and `package_digest`, and includes the pinned image/lock, capability approval,
artifact hashes, and log/manifest digests. Downloaded fixture artifacts are
reported as `quarantined_unreleased` custody records for normal worker artifact
intake; the bootstrap never releases, classifies, exports, or accepts them.
`scientific_results` is always `not_collected`: workspace readiness is not a
run, measurement, conclusion, or scientific result.

Retries reconcile stable effect keys. A partial failure can continue without
repeating already recorded effects; a lost reply remains `unknown` until an
explicit retry/reconciliation. Cancellation fences the next effect and cannot
make the environment ready. A future live adapter needs separate approval and
must preserve these execution/custody identities, enforce the same source and
byte limits, and remain outside default CI.
