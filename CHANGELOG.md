# Changelog

All notable changes are recorded in Git history while Tessera is a source-only
developer alpha. This file does not create release tags or compatibility
commitments.

## Unreleased

- Documentation now states the release-readiness boundaries, local-ledger
  quickstart, recovery limits, and synthetic-example scope.
- Current local schema: v43. Schema v43 records operation-bound remote workflow
  heartbeat, checkpoint, and reconnect/outage metadata for new packages only; it
  does not enable remote execution or operational failover. See [implementation progress](docs/implementation-progress.md)
  and the [compatibility matrix](docs/compatibility-matrix.md) for evidence and
  remaining gates.
- Local backups include committed project bytes in a versioned object bundle;
  restore remains permanently read-only held history and does not fence the
  original service, workers, or external jobs.

## Historical evidence

Historical implementation checkpoints and dated test evidence remain in
[docs/implementation-progress.md](docs/implementation-progress.md). They are
not retroactively relabeled as release validation.
