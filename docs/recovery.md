# Local control-state backup and held recovery

Schema 11 supports consistent local SQLite control-state backups. This recovery
slice preserves history for inspection; it does not provide operational failover.

Schema-v24 local filesystem project objects are outside SQLite. Current backups
use a versioned bundle when a committed local object exists: one SQLite snapshot,
the exact referenced object bytes, and a declaration of their custody/retention
metadata and SQLite-only legacy artifacts. Object namespaces and keys are verified
against the snapshot, then their regular-file size and SHA-256 are checked before
publication and again during restore. A deleted object is declared as unavailable;
it is never fabricated. A missing, corrupt, substituted, or unsafe required object
fails the backup or restore closed. Retention metadata records manual tombstones,
deletion intent, activity, and degraded/missing-object incidents; it never starts
deletion automatically.

Create a backup while the local control service is running:

```sh
uv run --locked tessera backup --state-dir .tessera --output /safe/place/tessera-backup
```

`--output` must name a new directory. Tessera creates it privately. SQLite-only
backups retain the original `snapshot.sqlite3` and `manifest.json` layout. A bundle
with local project objects additionally contains private `objects/` bytes and a
format-v2 manifest. SQLite's online backup API makes one consistent point-in-time
copy while the source may continue to accept work. It includes control records and
retained SQLite BLOBs, artifact manifests, custody/retention metadata, research
links, upload state, labbook history, and execution/accounting history. It does not
copy `owner.header`, SQLite journals, provider jobs, or runtime journals outside the
control database.

The manifest records the backup ID, time, schema version, event watermark, snapshot
size, and SHA-256 digest. Bundle manifests additionally declare every external
object and every SQLite-only legacy artifact. Backup validates integrity and foreign
keys before publishing the manifest. The 1 GiB total bundle limit, 1,024-object
limit, 16 MiB per-object limit, and 30-second deadline are cooperative limits; an
in-progress kernel I/O operation cannot be preempted. Failed attempts leave their
private incomplete output directory for diagnosis.

Restore only into an absent destination:

```sh
uv run --locked tessera restore --backup /safe/place/tessera-backup --state-dir /safe/place/tessera-restored
```

Restore rejects incomplete, altered, unsupported, symlinked, hard-linked, FIFO, or
unsafe inputs and refuses existing destinations. It validates the copied snapshot,
the bundle declaration, and all required external object hashes before publishing
the new database. The destination receives a new private `owner.header`; its token
is not printed. Every restored principal and credential is revoked, including prior
owner, human-session, control-agent, and worker credentials.

Schema validation admits only the exact trusted bootstrap definitions, including
v36's cancellation fence trigger for the Kubernetes dispatch ledger. An added,
removed, renamed, or altered trigger is rejected before restore authority fencing;
recovery never permits arbitrary backup-supplied executable schema.

The restored database enters permanent `held_restore`. Its fresh owner can read APIs
and the desktop can show recovered history, but every mutation and replay is refused.
There is no CLI, API, or desktop resume/clear-hold operation. `GET /recovery` reports
restore lineage, watermark, unresolved grant metadata, and the hold.

Restore does not stop or fence the original control service, workers, or provider
jobs. The original branch is unfenced and post-backup activity is unknown to the
restored copy. Historical grants, leases, observations, reservations, settlements,
and accounting remain records; they do not prove provider termination or reconciled
spend. The held copy has no dispatch, lease-renewal, replay, or cleanup authority.
Reconcile external operations before considering replacement work.

If restore fails, Tessera leaves its private incomplete destination in place rather
than deleting a path that may contain evidence. Do not reuse it as a restore target.
Expected CLI failures return status 1 with a concise stderr message and no success
output. Successful restore prints only the database and credential-file paths.

Recovery does not bootstrap or attach Kubernetes targets, reapply provisioning, or
re-materialize credentials. Any future reconciliation uses the explicitly recorded
target identity and fresh human authorization; it must not use an ambient context
or automatically destroy resources.
