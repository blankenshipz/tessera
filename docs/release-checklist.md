# Release-readiness checklist

This checklist records gates for a future release. Checking an item requires
dated, reviewable evidence; it does not authorize a deployment or provider use.
Sanitized local evidence IDs identify retained local records only; they do not
make ignored files public proof or package inputs.

The [launch-readiness contract](launch-readiness.md) separates locally enforced
intake/admission safeguards from unresolved identity, aggregate storage and
provider-spending gaps. Its tests do not check off any external release gate.

## Current source and candidate status (2026-10-08)

The source migration ceiling is schema 55. A fresh `0.1.0rc2` wheel install
initialized schema 55; bounded installed checks and a three-Job local Kubernetes
slice also ran against that exact candidate. Synthetic identities/data and all
remaining live-acceptance limits are summarized in the
[sanitized candidate evidence note](release-evidence-intent9.md). Earlier v5/v8
receipts remain historical at their exact artifact revisions.

The 14-file `release/opencode-observer/runtime-v8/` bundle is now integrated into
the package input rules. Its closed manifest pins 50 originals/79 targets, public
dependencies, licenses and reconstruction checks; it is a sparse public source
closure, not a full OpenCode checkout or verified upstream commit. Packaging phases
2–3 report 40 distinct scoped checks passing across retained runs (31 bundle/package
checks plus nine starter-resource checks; composite across separate invocations, not
one suite). Phase 3 includes six exact starter example resources in wheel/source
rules and installed-resource validation. The measured candidate is `0.1.0rc2`, built
twice byte-identically; its source and wheel hashes and exact-source gate are in the
evidence note. It is not a release approval. Newer CLI/factory and documentation work
is in progress, so those changes are not covered by this artifact. After writers
converge, the root must freeze a new complete inventory, build/install that candidate,
and run applicable exact-revision gates. The earlier 792-input pending-list SHA
`6ce9689f91226115b738e4a39e28ca67ae03ac5d27643f446c8d0b1b37bcd0a0` is superseded
and is not a frozen root inventory. See the
[operator setup boundary](runtime-operator-setup.md),
[source-deliverable runbook](opencode-source-deliverable.md),
[dataset snapshot contract](dataset-snapshots.md), and
[tabular starter status](tabular-starter.md).

The earlier one-Job starter run remains historical and is not a substitute for the
later candidate-bound three-Job slice. See the sanitized evidence note for its
restart/cancellation boundaries and synthetic-data limits. Do not link ignored
private receipts as public proof.

Do not call the candidate a release. Although the measured artifact passed the
listed exact-revision checks, the current source/docs tree has moved on; the final
freeze must include all later edits, fresh installed checks and any affected protocol,
native and source gates. A source inventory or patch hash alone is not package
parity, upstream provenance, legal review or universal secret-free proof.

## Before any public release

- [x] Maintainer selected MIT; LICENSE and package metadata record the terms.
- [ ] Verify/enable the selected GitHub private vulnerability-reporting channel.
  A recent authenticated API read returned HTTP 404 for the private repository's
  reporting endpoint; availability/enablement remains unknown, not disabled.
  See [SECURITY.md](../SECURITY.md). The maintainer must verify an available private
  channel before accepting sensitive reports; do not invent a public contact or
  change repository visibility as a workaround.
- [ ] Release owner defines support, privacy, retention, backup, and owner/account
  recovery commitments appropriate to the release. Current docs establish no
  support-response SLA, retention period, backup service level, aggregate storage
  guarantee or supported account-recovery promise.
- [ ] Supported operating systems, Python/runtime versions, installation paths,
  upgrade and rollback procedures have reproducible evidence.
- [ ] Release notes identify the exact schema version and upgrade/backup path.
- [ ] Reproducible build/dependency provenance, security review, artifact signing
  and update verification are evidenced for the release artifacts, not only source.
- [ ] Reconcile the exact OpenCode/native source inventory with the public packaging
  allowlist, both licenses, source-manifest, patch reconstruction and shipped source
  archive; record upstream identity as unknown where it is not verified.

## Before claiming a production workflow

- [ ] Complete the selected public/simple CPU baseline-plus-intervention workflow
  with remote control/execution that survives desktop disconnection, linked
  evidence, all terminal outcomes, and human review.
- [ ] Validate persistent output storage, restore/reconciliation, retention, and
  artifact recovery on supported infrastructure.
- [ ] Complete provider-specific dispatch, credential, cost, cancellation, and
  incident-reconciliation gates without billing fallback.
- [ ] Validate the selected OpenCode build and any Kubernetes target separately;
  synthetic seams and pure contracts are not compatibility evidence.
- [ ] Establish operational ownership, monitoring, recovery, and security review.
- [ ] Verify production human sessions, privileged reauthentication, owner rotation,
  organization revocation and scoped workload credential lifecycle. Local bearer
  possession is not production organization authentication.
- [ ] Verify TLS/ingress isolation, forwarded-header trust, connection/header/rate
  limits, secret-safe logs and authenticated-response cache policy on the target.
- [ ] Enforce aggregate storage capacity across database, objects, staging, journals
  and backups; test disk/inode exhaustion. Per-request/per-lease caps are insufficient.
- [ ] Define operator-owned quarantine, deletion/disposition, backup-custody and
  restore/reconciliation procedures for dataset bytes, artifacts, journals and
  held history. Existing retention metadata and held restore are not deletion policy,
  active failover, or aggregate capacity guarantees.

Current details and historical evidence remain in
[implementation progress](implementation-progress.md) and the
[compatibility matrix](compatibility-matrix.md).

## Distribution checks

From a trusted checkout, use `uv build --out-dir /private/release-build` and run
`python dev/check_distribution.py /private/release-build/ARTIFACT` once for the
wheel and once for the source tarball. The check installs without inherited
credentials or Python paths, checks the inventory/license and dependencies,
including the optional MCP extra, installed CLI help, skill and tool/resource
declarations. It starts only its own ephemeral loopback service for private initialization,
authentication denial, project creation, backup and held restore. Its JSON
includes the observed artifact hash and schema. It does **not** verify Docker,
Kubernetes, live MCP protocol/client calls, native desktop, credential renewal
or independent-host operation.
Run final artifact checks after all writers have converged; a preliminary
distribution is not the shipped revision.
