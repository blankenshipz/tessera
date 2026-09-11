# Release-readiness checklist

This checklist records gates for a future release. Checking an item requires
dated, reviewable evidence; it does not authorize a deployment or provider use.

## Before any public release

- [ ] Maintainer selects and adds release terms separately. This repository does
  not select or imply a license.
- [ ] Maintainer configures a private security-reporting channel in SECURITY.md.
- [ ] Release owner defines support, privacy, retention, backup, and owner/account
  recovery commitments appropriate to the release.
- [ ] Supported operating systems, Python/runtime versions, installation paths,
  upgrade and rollback procedures have reproducible evidence.
- [ ] Release notes identify the exact schema version and upgrade/backup path.

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

Current details and historical evidence remain in
[implementation progress](implementation-progress.md) and the
[compatibility matrix](compatibility-matrix.md).
