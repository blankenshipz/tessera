# 0114 — Control database compaction

## Status

Proposed (no schema change; the schema version is unchanged). Builds on the backup bundle of
`tessera backup` / `tessera restore` and on the retention policy of
[ADR 0094](0094-storage-retention-policy.md).

## Context

The control database is a SQLite file. Retention and maintenance delete rows (old backups'
metadata, expired leases, reclaimed artifact chunks), but SQLite keeps the freed pages on a free
list and reuses them later; the file itself never shrinks. After a large cleanup the file can be
mostly free pages while still occupying its full size on disk, and a full disk stops the control
plane. Compacting by hand with ad-hoc `sqlite3` commands is risky: there is no check that the
service is stopped, no check that there is room for the copy, no verification that the new file
holds the same data, no kept copy of the old file, and no record that it happened.

## Decision

1. **One operator command.** `tessera db compact --state-dir DIR [--backup-root DIR]
   [--min-free-bytes N] [--dry-run]`. `--dry-run` runs the preconditions and prints the plan
   (sizes, freelist pages, an estimate of the size after, free disk) and writes nothing,
   not even the lock file. Its lock check is weaker than a real run's: it shows only that the
   maintenance lock is free and that no writer is committing at that moment, because proving
   that no other process has the database open needs a write lock. A run
   prints its receipt as JSON.
2. **Services must be stopped; what is enforced.** The long-running processes that keep the
   control database open hold a shared advisory `flock` on `<state-dir>/.maintenance.lock` for
   their whole life and refuse to start (exit 1) while a compaction holds the exclusive lock:
   `tessera serve` and the Salad campaign executor (`tessera-salad-executor`, including
   `--once`). Compaction takes the exclusive lock without blocking and refuses while either of
   them is running. Other entry points either have their own database (the Salad guardian, the
   A2A peer, the reasoning runtime, workers) or reach the control plane over HTTP. Short-lived
   commands that open the control database directly (`tessera migrate`, `backup`, `restore`,
   `retention`, the executor enrollment renewal script) do not take the lock; they are covered
   by the writer guard instead: compaction holds a `BEGIN IMMEDIATE` write transaction on the
   source from the copy through verification and the swap, and re-checks the source's
   fingerprint before swapping, so a writer that arrives during a run fails with a busy error
   rather than having its write lost. A long-lived process that opens the database without
   taking the lock is not protected: operators must stop it before compacting. The lock is
   advisory and local: it catches a forgotten running service, not a hostile process.
3. **Refusals before any write.** Compaction refuses a database in the `held_restore` state, a
   database or state directory that is a symbolic link, a database that needs
   `tessera migrate` first, and a run with no backup root (the `backup_root` of the
   storage-retention policy, or `--backup-root`).
4. **Free-disk floor first.** Before writing anything it checks free space on the state
   directory's filesystem and on the backup root's. The temporary file is created in the same
   directory as the database, so the final rename stays on one filesystem and is atomic. The state
   filesystem must have at least `--min-free-bytes` free, by default `DEFAULT_MIN_FREE_BYTES`
   (8 GiB) and never less than the size of a copy with headroom (when the backup root is on the
   same filesystem, room for the backup bundle is added); the backup root must have room for a
   copy of the database plus a 1 GiB floor. The floor is a default, not a measurement of any
   particular machine. A database larger than the largest permitted backup is refused.
5. **`VACUUM INTO` and verification.** While the write lock of decision 2 keeps writers out, the
   new database is built with `VACUUM INTO` a private (mode 0600) temporary file, never in place.
   Before it is used it must pass restore's own snapshot validation, `integrity_check` and
   `foreign_key_check`, and match the source: the same row count in every table, equal
   `schema_meta` and `control_recovery` rows, and the same event cursor watermark. The journal
   mode (WAL or not) is carried over.
6. **The previous database is kept as a restorable backup.** The pre-compaction database is
   written as a standard backup bundle (`snapshot.sqlite3` plus `manifest.json`, directory
   `pre-compact-<UTC timestamp>-<run prefix>`) under the backup root, before the swap, and must
   pass restore's validation. When the backup root is the retention policy's `backup_root`,
   storage retention lists, keeps and ages it like any other backup, and `tessera restore
   --backup` can restore it. With a different `--backup-root` the bundle is still restorable but
   retention does not manage it, and the receipt says so.
7. **Atomic swap and rollback.** The old file is hard-linked aside, the verified temporary file
   replaces the database with `os.replace`, and the result is verified again (integrity, foreign
   keys, parity). If that verification fails the old file is renamed back and the run reports
   failure; the bundle is kept. A failure before the swap removes the temporary file and the
   bundle, leaving the database untouched.
8. **A receipt, not an event.** Each completed run writes a private JSON receipt to
   `<state-dir>/db-compact/<UTC timestamp>-<run_id>.json`: bytes before, after and reclaimed,
   page counts and freelist pages, page size, per-table row counts, duration, free-disk numbers,
   the backup's path, id, sha256 and size, and the status. It is not a control-database event: the
   database it would be recorded in is the one being replaced, and the service is stopped.
9. **Health recommendation.** `GET /operator/health` gains an additive `database` object with
   freelist statistics, and a `db_compaction_recommended` warning alert only when the reclaimable
   space is at least 64 MiB (`FREELIST_MIN_BYTES`) and at least 25% of the file
   (`FREELIST_MIN_FRACTION`), module constants in `db_compaction_health.py`. The alert recommends;
   it never compacts.
10. **No schema change.** No table, column or migration is added.

## Consequences

- An operator can reclaim space after a large retention run with one command that refuses unsafe
  states, proves the data is the same, and leaves a restorable copy and a receipt.
- The control plane is down for the duration of the run, and the run needs disk for roughly two
  copies of the database (the temporary file and the backup bundle). A database on a
  nearly full disk is refused rather than attempted.
- The kept pre-compaction backup is subject to the retention policy; it is aged out like any
  other backup unless it is the newest verified one.
- Health output gains a field and a possible warning; existing readers are unaffected.
- The advisory lock does not stop a process that ignores it, and only the processes listed in
  decision 2 take it.

## Alternatives

- **Online `VACUUM` in place.** Rewrites the file under the live database, needs a full copy of
  free space anyway, blocks writers, and a crash mid-run is hard to reason about. Rejected.
- **`PRAGMA auto_vacuum`.** Changing the mode of an existing database needs a `VACUUM` anyway, and
  full auto-vacuum moves pages on every commit, adding write cost to the hot path. Rejected.
- **`PRAGMA incremental_vacuum`.** Only helps a database already in incremental auto-vacuum mode,
  which ours is not, and still needs a scheduler and a policy for when to run. Deferred.
- **Copy the live file and truncate in place.** Cannot be verified before it replaces the data,
  and a failure leaves no good copy. Rejected.

## Open questions

- Online compaction while the service runs, and what it would need from the write path.
- Scheduling: whether a timer (like the retention timer) should ever run compaction, or whether
  it stays an operator action prompted by the health alert.
- Write-ahead-log details: how journal mode and any `-wal` and `-shm` files interact with the
  swap, and whether a checkpoint step should be recorded in the receipt.
