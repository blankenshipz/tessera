# Local control-state backup and held recovery

Schema 11 supports consistent local SQLite control-state backups. This recovery
slice preserves history for inspection; it does not provide operational failover.

Create a backup while the local control service is running:

```sh
uv run --locked tessera backup --state-dir .tessera --output /safe/place/tessera-backup
```

`--output` must name a new directory. Tessera creates it privately and writes only
`snapshot.sqlite3` and `manifest.json`. SQLite's online backup API makes a
consistent point-in-time copy while the source may continue to accept work. It
includes control records and retained SQLite BLOBs, including quarantined artifacts,
upload state, labbook history, execution/accounting history, and research records.
It does not copy `owner.header`, SQLite journals, provider jobs, or runtime journals
outside the control database.

The manifest records the backup ID, time, schema version, event watermark, snapshot
size, and SHA-256 digest. Backup validates integrity and foreign keys before
publishing the manifest. The 1 GiB size limit and 30-second deadline are cooperative
limits; an in-progress kernel I/O operation cannot be preempted. Failed attempts
leave their private incomplete output directory for diagnosis.

Restore only into an absent destination:

```sh
uv run --locked tessera restore --backup /safe/place/tessera-backup --state-dir /safe/place/tessera-restored
```

Restore rejects incomplete, altered, unsupported, symlinked, or unsafe inputs and
refuses existing destinations. It validates the copied snapshot before publishing
the new database. The destination receives a new private `owner.header`; its token
is not printed. Every restored principal and credential is revoked, including prior
owner, human-session, control-agent, and worker credentials.

The restored database enters permanent `held_restore`. Its fresh owner can read APIs
and the desktop can show recovered history, but every mutation and replay is refused.
There is no CLI, API, or desktop resume/clear-hold operation. `GET /recovery` reports
restore lineage, watermark, unresolved grant metadata, and the hold.

Restore does not stop or fence the original control service, workers, or provider
jobs. The original branch is unfenced and post-backup activity is unknown to the
restored copy. Historical grants, leases, observations, reservations, settlements,
and accounting remain records; they do not prove provider termination or reconciled
spend. Reconcile external operations before considering replacement work.

If restore fails, Tessera leaves its private incomplete destination in place rather
than deleting a path that may contain evidence. Do not reuse it as a restore target.
Expected CLI failures return status 1 with a concise stderr message and no success
output. Successful restore prints only the database and credential-file paths.
