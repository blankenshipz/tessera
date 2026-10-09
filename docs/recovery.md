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
restore lineage, watermark, unresolved grant metadata, and the hold. Its `promotion`
contract is deliberately non-executable: `supported` is always `false`, and the only
permitted path is `manual_fenced_rebuild_only`. A replacement active service requires
an operator to fence the original writer, reconcile recorded external operations,
enroll fresh identities, and obtain fresh human authority. Tessera has no restore
promotion command and a held copy cannot be relabelled active.

Held database bytes are never changed for denials or backup health. Those operational
records use a separate sibling sink and are labeled as such in operator observability;
if that sink is unavailable, Tessera reports the recording failure without changing
the held source.

Restore does not stop or fence the original control service, workers, or provider
jobs. The original branch is unfenced and post-backup activity is unknown to the
restored copy. Historical grants, leases, observations, reservations, settlements,
and accounting remain records; they do not prove provider termination or reconciled
spend. The held copy has no dispatch, lease-renewal, replay, or cleanup authority.
Reconcile external operations before considering replacement work.

## Stale local-pilot workers and expired reservations

`GET /operator/health` distinguishes an unavailable budget caused entirely by
unattempted authority (`resource_budget_unattempted_authority_reserved`) from
settled or attempted exposure (`resource_budget_exhausted`). Both block new
authority; the former is not evidence of provider spend or a completed attempt.
The projection retains any held amount that cannot be matched to an unresolved
grant as unattributed exposure and does not clear it.

For an owner-confirmed stale local worker, use
`POST /workers/{worker_id}/retire-stale` with an idempotency key and the current
worker revision. The endpoint refuses a fresh or invalid heartbeat. It records a
normal retirement event, revokes the worker credential, and retains the registry,
grant, lease, and audit history. It does not delete a workload, infer provider
termination, release budget, or create replacement authority. Journaled operations
remain fenced for separately authenticated recovery reconciliation.

An owner may use `POST /execution-grants/{grant_id}/reconcile-expired-unattempted`
with the current grant revision only after the grant is expired and only when the
control record has neither an execution lease nor an external-operation intent.
It records zero settlement and releases that reservation without claiming provider
termination. A lease or operation intent, including one without a receipt, blocks
this shortcut: use grant cancellation and the evidence-based recovery path instead.
These actions are local control-record changes; they do not contact a provider.

If restore fails, Tessera leaves its private incomplete destination in place rather
than deleting a path that may contain evidence. Do not reuse it as a restore target.
Expected CLI failures return status 1 with a concise stderr message and no success
output. Successful restore prints only the database and credential-file paths.

Recovery does not bootstrap or attach Kubernetes targets, reapply provisioning, or
re-materialize credentials. Any future reconciliation uses the explicitly recorded
target identity and fresh human authorization; it must not use an ambient context
or automatically destroy resources.

## Historical schema-50 post-migration verification gate (does not cover schema 55)

This gate verifies a schema-50 migration/restore workflow only. The current source
schema is 55, while the latest installed platform receipt is schema 52. Do not use
this dated schema-50 manifest requirement as release verification for a newer
artifact; an exact schema-55 migration/restore gate remains to be run against the
frozen candidate.

A pre-migration backup is rollback evidence, **not a backup of the migrated
labbook**. Restore requires the running schema version; use the matching older
release to restore an older backup, then explicitly migrate that held copy. Never
edit a manifest's schema version or the database recovery state to bypass this.

The v47-to-v48, v48-to-v49 and v49-to-v50 migrations clear the active state's
previous backup-success time and ID (retaining any recorded failure).
Backup freshness is unknown until a new
backup succeeds. Held migration preserves its recovery/credential/health records;
it does not grant authority. Repeating migration on current v50 state does not
clear a new backup. **Already-migrated v50 installations must still perform this
gate**: the migration is not rerun and a recent health timestamp alone is not
proof that the backup includes the new records. `backups.freshness: fresh` means
recent publication, not successful restore verification. No restore-verification
receipt is currently persisted in operator health; keep the evidence externally.

For an authorized lab upgrade, retain the pre-upgrade backup, explicitly migrate,
then complete these steps before declaring recovery verification complete:

1. Select the intended source explicitly. For Kubernetes, use the named
   kubeconfig/context/namespace and operator-approved single-writer procedure;
   never mount the production PVC in a second writer or use it as a drill target.
   Keep private backup outputs outside the source state/PVC. Quiesce application
   writes for an exact before/after comparison, or declare an explicit event
   cutoff and record IDs rather than comparing to a subsequently changing source.
2. After the MixFM records to be protected have committed, retain private baseline
   responses for `GET /studies/{id}/export`,
   `GET /studies/{id}/dataset-snapshots`, and
   `GET /interaction/studies/{id}/program-context`. Record their IDs, revisions,
   source/variant metadata, protocol/record/report digests, dataset schema and
   snapshot digests, source/license/provenance links, and review attribution.
   Record the last required event cursor. Check export completeness and pagination;
   do not treat a truncated projection as a complete baseline.
3. Create a **new post-migration backup** with the command above. Retain its
   manifest and require `schema_version: 50`, an `event_watermark` at least the
   last required cursor, and its backup ID, timestamp, size and SHA-256. A newer
   timestamp alone does not prove inclusion. The digest detects changed bytes,
   not malicious replacement of both manifest and snapshot; protect their custody.
4. Restore using the same release into a new private local drill directory,
   never the source or production PVC. Restore must exit successfully; it checks
   the exact schema, snapshot hash, SQLite integrity/foreign keys, watermark, and
   any required object bundle before publishing held state. Retain failed outputs
   for diagnosis; do not reuse their paths.
5. Inspect only the restored API, authenticated with its fresh `owner.header`
   through a private runtime reference (never print/copy tokens into the report).
   Compare the three projections to the recorded baseline, including attribution,
   event history and all digests. Dataset metadata declares provenance; it neither
   contains external dataset bytes nor proves licensing/content validation.
   Evaluation records remain caller-reported, not new execution evidence.
6. Require `/recovery` to show `held_restore`, `recovery_hold: true`,
   `promotion.supported: false`, and `source_backup.digest`/`event_watermark`
   matching the manifest. The original branch remains unfenced and later activity
   unknown. On this **disposable held target only**, verify old owner, human,
   agent and worker credentials return 401; the fresh owner may read but a valid
   metadata write/review and idempotent replay return 403. Restart the held app and
   repeat. Never test mutation denials on the active source. Do not clear holds,
   renew leases, replay submissions, or infer a writable promotion from this drill.
7. Retain a credential-free report with the release, backup manifest identity,
   cutoff, compared record IDs/digests, restore duration, checks and failures.
   Verify off-PVC custody separately. A local pass is not a PVC-loss drill,
   provider reconciliation, production RPO/RTO, or full integration acceptance.

### Bounded disposable developer drill

This single test invokes the real backup/restore CLI handlers against generated
temporary state with populated MixFM metadata, evaluation reviews and v48 dataset
records. It compares retained rows/API exports, checks SQLite integrity, rejects
old credentials, tests held writes/replays/restarts, and backs up/restores the held
copy again without writable promotion. It uses no network providers or PVCs:

```sh
.venv/bin/pytest -q tests/test_recovery_mixfm.py::test_v48_mixfm_cli_backup_restore_drill
```

The full `tests/test_recovery_mixfm.py` additionally checks migration freshness,
held migration, altered dataset bytes/schema/foreign keys, and incompatible
schema/watermark manifests. All inputs and credentials are disposable synthetic
fixtures; these checks do not attest to the contents of any live v48 smoke backup.
