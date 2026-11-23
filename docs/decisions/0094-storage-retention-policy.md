# ADR 0094: Storage retention policy for backups and unreferenced artifacts

Status: accepted

## Context

Backups (`tessera backup` bundles) and placed project artifacts only ever grew. A person
had to prune old backups by hand to free disk. Deletion of project artifacts existed only
as manual tombstone/delete commands (see `docs/recovery.md`). Deleting by hand is the work
a platform should own, but a policy that deletes must be as careful as the person was.

## Decision

An operator-owned policy file `storage-retention.json` in the state directory (strictly
validated, unknown keys refused, every knob optional) configures:

- `backup_root`: a directory holding one backup bundle per child.
- `backup_keep_last`: the newest N verified backups are always kept.
- `backup_max_age_days`: older backups beyond that floor are selected.
- `disk_free_min_bytes`: below this free space on the backup filesystem, the oldest backups
  beyond the floor are selected, oldest first, until the threshold is met.
- `artifact_max_age_days`: unreferenced project artifacts idle longer than this are selected.
- `auto_apply` (default false) and `timer_interval_seconds`.

With no policy, or no knob set, nothing is ever selected. The newest verified backup is
never selected whatever the policy says. A bundle that is incomplete, unreadable or a
symlink is skipped and reported, never deleted.

An artifact is never selected while anything live refers to it: a manifest, experiment
evidence, a result checkpoint, lineage, a campaign, or an unreleased hold; nor when its
retention class is `held` or its state is `degraded`/`deleted`. Unknown or malformed
metadata keeps it (fail closed). Deletion goes through the existing tombstone and delete
state machine of the project artifact retention store, so its checks still run in the same
transaction; the policy adds no bypass.

Every use lists first. `tessera retention plan` is a dry run that prints the plan and its
digest; `tessera retention apply --plan-digest D` deletes only if a fresh plan has exactly
that digest, so what is deleted is what the person saw. Each backup is re-verified (same
directory, not a symlink, same manifest id and snapshot digest and size) immediately before
removal.

The control plane runs a timer (`tessera serve`). By default it only writes a dry-run
receipt when the plan changes; it applies only when the operator set `auto_apply`.

Every run writes a private receipt (`storage-retention/<time>-<run>.json`, mode 0600): the
policy and plan digests, the full plan with each keep/delete reason, and for each deletion
its kind, id, size and digest, then the total bytes freed and any failure. The receipt is
written before the first deletion and rewritten after each, so a crash leaves a truthful
record. `GET /storage-retention/status` and the MCP tool `tessera_storage_retention_status`
report the last run, the pending dry run and totals, read only.

Artifact deletion acts as the project's local owner principal (the store requires a live
authenticated principal and its idempotency table references principals), with idempotency
keys derived from the run and artifact; the store's own tenant and liveness checks still
apply. A distinct system principal would need a migration and is left for later. Beyond
the store's own reference checks, selection also keeps anything named by lineage (by id or
digest), by an unreleased disposition, or by any table with an `artifact_id` column.

A backup counts only if `tessera restore` would accept it: retention runs restore's own
validation (manifest and layout, snapshot digest and integrity, declared objects and their
digests; the shared `recovery.verify_restorable`) before a bundle is listed. An unrestorable
bundle is skipped and reported, so it never uses up the keep-last floor, never protects itself
at the cost of an older good one, and the last restorable backup is never selected. Nothing is
cached, and hashing is bounded to the bundles whose verdict matters: the newest restorable
bundles that fill the keep floor (what must survive) and every bundle selected for deletion
(an unrestorable one is reported, never deleted). A bundle kept only because it is young or
inside the keep window is not read in full (its manifest is). Each pass re-hashes those
again, so a bundle that rotted in place, even keeping its size and modification time, is
never counted. Right before the first backup deletion of an apply, the bundles being kept are
re-hashed once more; if any of them no longer restores, nothing is deleted and the receipt is `refused`.
Deletion re-verifies the bundle it removes. Verification is tracked by directory, not backup id
(copies of one bundle share an id).

A bundle kept without being read in full is labelled `unverified` in the plan, and the timer
runs a full sweep (`backup_verify_interval_hours`, default 24; `tessera retention verify` by
hand) that receipts which bundles would no longer restore, so a rotted young bundle is
visible; status reports the last sweep (`backups`). A sweep of an unreadable backup root fails (receipt `status: failed` with the reason, never "nothing wrong"); a failing sweep is logged and never blocks the plan; sweep receipts (`*.verify.json`) keep the newest 14.

A refused apply (stale digest, no policy, `auto_apply` off) also writes a receipt, with
`status: refused` and the reason. A failed deletion records its reason and, for an artifact
refused after its tombstone, `state_after: tombstoned` (reserved, not deleted). A stage input
or output of any campaign, by digest, keeps an artifact.

Known limit: under disk pressure the free-space reading shapes the plan, so its digest can
change between listing and apply; apply then refuses and the plan is listed again.

### Orphaned large artifacts (weights archives)

A multi-gigabyte output is stored as chunk rows in the control database (ADR 0050). One that
no manifest lists, whose lease is over and that is older than the operator's orphan age (or
whose every manifest was rejected) was only ever freed by a person's reclaim call or an
opportunistic worker sweep, so such archives stayed recorded and undeleted. Retention now
lists them as `large_artifact` candidates and frees them with the existing atomic reclaim
(its reason is re-checked under the write lock, it refuses while control is held after a
restore, and the quarantine row stays as evidence). It is opt-in
(`reclaim_orphaned_large_artifacts: true`), never shorter than the operator's orphan age,
dry run first like everything else, and each reclaim is on the receipt with its size and
digest.

### Platform agents

An external agent credential enrolled with `retention:read` may read status and list a plan
(a dry run, receipted); one enrolled with `retention:apply` may apply a listed plan by its
digest. Agents never set the policy or choose what is deleted: the operator's policy file
selects, the digest binds the apply to what was listed, and a stale digest is refused and
receipted. MCP tools: `tessera_storage_retention_status`, `_plan`, `_apply`.

### Deploy-upgrade backups

`tessera deploy-upgrade` takes, before it migrates, either a validated `tessera backup` bundle
(when the database is already at the release's schema) or a pre-migration snapshot (an older
schema that `restore` refuses): `snapshot.sqlite3` plus `snapshot-manifest.json`, and, beside it,
a `<name>-configs` directory of config copies. Retention manages all of them in one
backup root. A snapshot is restorable when the directory is exactly those two files, the database
matches the recorded size and digest, and SQLite reads it read-only at the recorded schema
version; its id is the digest's first 32 digits and its time the `backup-<stamp>` of its name. The
config copies are listed with, counted in the size of, and removed with their backup. The keep-last
floor and the "never the last restorable backup" rule apply across both formats.

Config copies are only a backup's companion when the backup is named `backup-<stamp>` and the
`<name>-configs` directory is a plain directory holding nothing but regular `config-<n>.json`
files; any other directory is never touched with the backup (it is reported). A snapshot is
opened read-only and immutable, so checking it never writes `-wal` or `-shm` files into the backup,
and a snapshot whose schema is newer than this release supports is not restorable. If the config
copies cannot be removed after the backup is deleted, the receipt row says so (`companion: not
removed: ...`) and counts only the bytes actually freed.

Verification of a bundle now happens in place. Restore copies a backup into a temporary
directory before it trusts it; doing that for every retention check staged ~5.7 GB deploy
backups into the temporary filesystem, which filled and raised `OSError`, so restorable backups
were reported as unrestorable and the disk kept filling. The size, digest, schema, declared
objects and layout checks are the same; only the copy is gone. A reason that names an OS error
now carries the errno name (for example `ENOSPC`), never a path.

## Consequences

- Receipts are files, not database rows: no schema change, and they are outside backups, so
  restoring a backup does not resurrect or rewrite retention history.
- The policy cannot reach outside `backup_root` or the artifact store.
- Not covered: remote or object-store backups; retention of receipts themselves; the live
  disk-pressure path is tested with an injected free-space reading, not a full disk.
