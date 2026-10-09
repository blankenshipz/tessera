# Single-active lab operations and recovery

This runbook applies only to the provider-disabled Kubernetes lab profile. It is
not an HA deployment, a failover procedure, or a production availability claim.
One Deployment replica and one `ReadWriteOnce` PVC are the required writer
boundary. Do not scale the Deployment, attach the PVC to another Pod, or run a
second control process against the same state directory.

`GET /operator/health` and `GET /operator/audit` require the local owner. They
are metadata-only views: audit payloads omit bearer values, token hashes, secret
references, request bodies, and provider credentials. Health alerts report stale
worker heartbeats, open recovery-reconciliation records, the latest locally
recorded backup failure, and configured project monetary/concurrency budgets.
They do not account for provider usage, prove external-job termination, or fence
a writer.

## Before any procedure

1. Record the exact namespace, Deployment, PVC, image digest, state path and
   current `/health/ready` response in the incident record. Do not copy an owner
   header, bearer credential, secret, ConfigMap values, or artifact bytes there.
2. Read `/operator/health`. Treat `worker_heartbeat_stale`,
    `reconciliation_backlog`, `backup_failed`, `backup_stale`,
    `backup_unverified`, and exhausted resource-budget alerts as open operational
    work; do not admit replacement work to resolve an alert by itself.
    `backup_stale` is a 24-hour local warning threshold, not an RPO claim.
3. Identify all active/cancel-requested grants and external operation IDs from
   authenticated control records. A Pod restart, PVC detach, or a cancellation
   request is not evidence that a worker, provider job, or writer has stopped.
4. Take a fresh backup where the source is readable. Store the output outside
   the PVC and validate it with the restore procedure below.

## Process restart (PVC remains intact)

1. Confirm the profile remains `replicas: 1`, Deployment strategy `Recreate`,
   and the PVC is the one recorded in the incident. Do not use a second manual
   Pod to investigate the state directory.
2. Restart the single Deployment using the explicitly selected kubeconfig,
   context, namespace and Deployment name. Wait for the old Pod to terminate
   before considering the new Pod the only writer.
3. Require `/health/live` and `/health/ready` from the new Pod. Then compare the
   opaque backend identity and a pre-recorded read-only project/event cursor with
   the pre-restart values. A changed backend identity, unavailable state, or
   schema mismatch is a stop condition.
4. Re-read `/operator/health`; stale workers and recovery backlog remain alerts
   until reconciled. Do not infer a worker or provider restart from control
   process availability.

The opt-in restart harness is
`scripts/check_single_active_lab_recovery.py --run ...`; it delegates only to the
existing provider-disabled Docker Desktop restart fixture. It creates a new
fixture namespace and must never be pointed at an existing lab namespace.

## PVC loss or corruption

1. Stop the single Deployment and preserve the failed PVC and Pod metadata for
   forensic review. Do not delete, rebind, or mount the original claim from a
   second writer while its ownership is uncertain.
2. If a verified backup exists, restore it only to a new, absent directory with
   `tessera restore --backup BACKUP --state-dir NEW_STATE`. The result is a
   permanently `held_restore` history copy with fresh credentials and all prior
   credentials revoked *in that copy*.
3. Inspect the held copy and reconcile every original external operation using
   recorded identity. The held copy cannot dispatch, renew a lease, replay a
   command, clear the hold, or fence the original branch.
4. This release has no writable promote-restore path. Creating a replacement
   active lab requires a separately authorized rebuild, fresh enrollment and
   fresh human execution authority after reconciliation. Never attach the held
   copy to a running control Deployment as if it were recovered authority.

## Backup verification and restore drill

After migration, follow the [historical schema-50 post-migration verification gate](recovery.md#historical-schema-50-post-migration-verification-gate-does-not-cover-schema-55)
as well. A pre-upgrade backup or a recent backup-health timestamp cannot prove
that new MixFM records are protected; require a post-upgrade snapshot and compare
the metadata, evaluation and dataset projections on a disposable held restore.
Already-migrated v48 labs need this gate too. Never use the production PVC as a
restore-drill destination or interpret this check as writable promotion.

1. Run `tessera backup --state-dir SOURCE --output NEW_BACKUP`; the destination
    must be new and outside the source state directory. The local health
    projection records a successful backup only after publication, reports its
    age and a 24-hour freshness threshold, and exposes open recovery records by
    state plus their oldest opening time. If the source database cannot be opened,
    it cannot persist a backup-failure health record; the CLI failure and external
    operator monitoring remain required.
2. Restore only into a new drill directory. Check `GET /recovery` reports
   `held_restore`, `recovery_hold: true`, and unresolved grants; verify a mutation
   is refused. This validates preserved history, not a writable recovery.
3. Keep the drill state private, record backup ID/time/schema/watermark and
   destroy neither original evidence nor the failed restore directory
   automatically. The recovery command intentionally retains incomplete output
   for diagnosis.

## Writer fencing and active-work recovery

1. Fence new authority on the original live branch by requesting cancellation
   of each affected grant and revoking/retiring compromised worker credentials
   through the authenticated control API. Record desired cancellation separately
   from confirmed termination.
2. Quiesce human writers operationally: stop the old control Pod, confirm no
   second Pod or process has the PVC/state directory, and preserve any unknown
   worker/provider state as unresolved.
3. Use operation identity for lookup/cancellation reconciliation. Do not submit
   a replacement merely because a reply was lost, a credential was revoked, or a
   restore was created. Only attributable confirmed terminal evidence or explicit
   unknown evidence closes recovery.

## RPO and RTO

There is **no declared RPO or RTO/SLO** for this local single-active profile.
SQLite backups are point-in-time copies, have cooperative 1 GiB/30-second limits,
and do not include provider jobs or runtime journals outside the database. The
effective recovery point is the timestamp/watermark of the last verified backup;
post-backup activity is unknown in a held restore. Recovery time includes manual
writer fencing, operation reconciliation, replacement enrollment, and fresh
authorization, so it is intentionally unbounded until an operator measures a
drill for a named storage class and procedure.

Before claiming an RPO/RTO for a lab, record the backup cadence, verified restore
duration, object count/size, reconciliation duration, storage-class behavior and
all failures. A passing default test or opt-in fixture is not a service guarantee.
