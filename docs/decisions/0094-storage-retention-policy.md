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
visible; status reports the last sweep (`backups`).

A refused apply (stale digest, no policy, `auto_apply` off) also writes a receipt, with
`status: refused` and the reason. A failed deletion records its reason and, for an artifact
refused after its tombstone, `state_after: tombstoned` (reserved, not deleted). A stage input
or output of any campaign, by digest, keeps an artifact.

Known limit: under disk pressure the free-space reading shapes the plan, so its digest can
change between listing and apply; apply then refuses and the plan is listed again.

## Consequences

- Receipts are files, not database rows: no schema change, and they are outside backups, so
  restoring a backup does not resurrect or rewrite retention history.
- The policy cannot reach outside `backup_root` or the artifact store.
- Not covered: remote or object-store backups; retention of receipts themselves; the live
  disk-pressure path is tested with an injected free-space reading, not a full disk.
