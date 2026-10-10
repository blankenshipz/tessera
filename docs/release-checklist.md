# Maintainer release checklist

Tessera is a developer alpha. This is a checklist for a **specific candidate**,
not a running journal or release approval. Store dated private receipts outside
package inputs and bind results to exact source/artifact hashes. Documentation,
unit tests and readiness responses cannot establish live support.

## Freeze and package

1. Reconcile current docs, support/privacy/recovery commitments and compatibility
   requirements; stop package-input writers and freeze a reviewed input inventory.
2. Review the source allowlist, dependency/license inventory and closed observer
   bundles. Retain original attribution and source/provenance pins; do not infer
   upstream identity from copied-file hashes.
3. Build twice with the same frozen inputs/epoch and compare hashes using
   [candidate packaging](release-candidate.md). Do not ship private credentials,
   runtime state, logs or receipts. Changed inputs invalidate affected old gates.
4. Run [source gates](release-candidate-gates.md) and
   [protocol checks](protocol-compatibility.md) against that exact candidate.
   Passing packaging does not prove live compatibility or release readiness.

## Install and upgrade

From a trusted checkout:

```sh
uv build --out-dir /private/release-build
python dev/check_distribution.py /private/release-build/ARTIFACT
```

Run the distribution check once for the wheel and once for the source tarball.
It checks inventory/license, dependencies including MCP, entrypoint help,
portable skill/resources and a private ephemeral loopback control workflow:
fresh initialization, authentication denial, project creation, backup and held
restore. Its observed hash/schema are artifact-bound. It does not verify Docker,
Kubernetes, live SDK clients, native desktop, renewal or independent-host use.

- Verify the supported OS/runtime/install matrix and explicit migration on
  representative old state. Preserve matching old binaries and pre-upgrade
  backups; verify a new post-migration backup/held restore with the running schema.
- Record rollback/rebuild procedures without downgrading against newer state or
  clearing restore holds. See [recovery](recovery.md).
- Verify reproducible dependencies, independent security review, artifact signing
  and update verification before claiming a supported distribution.

## Security and operational ownership

- Keep MIT terms and third-party notices intact. GitHub private vulnerability
  reporting is enabled; direct sensitive reports through [SECURITY.md](../SECURITY.md),
  never public issues. Confirm the channel remains available for the release.
- Name support/incident owners and explicitly define privacy, retention, backup
  custody and recovery commitments. No SLA, RPO/RTO, aggregate capacity or account
  recovery promise is established by the current alpha.
- Verify production human sessions, privileged reauthentication, owner rotation,
  organization revocation and scoped workload identity lifecycle. Local bearer
  possession is not production organization authentication.
- Verify TLS/ingress, inaccessible direct backend, forwarded-header trust,
  connection/header/rate limits, secret-safe logs and authenticated-response cache
  policy on the target.
- Enforce and test aggregate database/object/staging/journal/backup capacity and
  inode limits, disk-full handling, quarantine, deletion/disposition and recovery.
  Request caps and declared PVC sizes are not aggregate quota evidence.

## Before claiming an integrated production workflow

- Complete a representative baseline/intervention journey with linked evidence,
  adverse/unknown outcomes and separate human scientific review.
- Verify an independent control/execution host, actual isolation/storage,
  restart/cancellation and work continuing while laptop/desktop is unavailable.
- Exercise genuine supervised planner/critic reasoning and real SDK transports,
  not deterministic/reference-only responses; verify exact installed bindings.
- Complete provider-specific credential, usage/cost, cancellation and incident
  reconciliation with no billing fallback. Internal reservations are not spend caps.
- Verify native picker/save and installed client reconnect on supported devices.
- Measure researcher effort and scientific quality; execution success is neither
  scientific validity nor a product-demand claim.

Keep unrun/failed/skipped gates explicit. Current boundaries are summarized in
[capabilities](implemented-capabilities.md) and [compatibility](compatibility-matrix.md).
