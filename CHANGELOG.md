# Changelog

All notable changes are recorded in Git history while Tessera is a source-only
developer alpha. This file does not create release tags or compatibility
commitments.

## Unreleased

- Documentation now states the release-readiness boundaries, local-ledger
  quickstart, recovery limits, and synthetic-example scope.
- Current local schema: v47. Schemas v43-v47 add operation-bound remote-workflow
  and recovery records, Astra coordinator consumer availability, human artifact-read
  custody and immutable reproduction bundles, and secret-free local-owner operator
  audit/health metadata. They do not enable remote execution or operational failover.
  See [implementation progress](docs/implementation-progress.md) and the
  [compatibility matrix](docs/compatibility-matrix.md) for evidence and remaining gates.
- Local backups include committed project bytes in a versioned object bundle;
  restore remains permanently read-only held history and does not fence the
  original service, workers, or external jobs.

## Historical evidence

Historical implementation checkpoints and dated test evidence remain in
[docs/implementation-progress.md](docs/implementation-progress.md). They are
not retroactively relabeled as release validation.
