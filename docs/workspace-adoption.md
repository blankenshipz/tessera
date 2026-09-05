# Preserve reviewed local inputs

Local adoption captures regular files into the control plane's private SQLite
state. The retained bytes and manifest are immutable through the application API;
later edits to the source folder cannot change the snapshot. Capturing files does
not execute them, attach running jobs, transfer data to a worker/model, or grant
execution authority.

Start with an initialized, migrated local state and an existing project. Run the
read-only inventory with an explicit absolute source path:

```sh
uv run --locked tessera workspace inventory --root /absolute/path/to/research
```

Review the returned file paths, sizes, hashes and exclusions. If saving this
output, save it outside the source folder so it does not change the next capture.
Copy the returned `digest` into the adoption command:

```sh
uv run --locked tessera workspace adopt \
  --root /absolute/path/to/research \
  --state-dir .tessera \
  --project-id PROJECT_ID \
  --expected-digest REVIEWED_DIGEST \
  --idempotency-key adoption-1
```

The local CLI reads the owner header through a bounded, no-follow file read. The
source root must not contain control-plane state. Adoption reads the files again
and compares the captured inventory to the reviewed digest. If anything in that
inventory changed, adoption fails and the inventory must be reviewed again. An
identical retry with the same key returns the original snapshot. Different
content cannot replace that record.

The transaction stores the captured bytes, complete inventory manifest, adopting
human, recorded timestamp, labbook event and idempotency response together. It
never rereads the source paths while persisting the captured bytes. The local
pilot stores at most 1,000 files and 16 MiB total per snapshot; an individual file
may be at most 16 MiB. Existing files are not overwritten. Snapshots are retained
with the control-plane state; there is no snapshot deletion endpoint yet.

Verify a retained snapshot after a restart or after the original folder changes:

```sh
uv run --locked tessera workspace verify \
  --state-dir .tessera --snapshot-id SNAPSHOT_ID
```

Verification rehashes the stored manifest and bytes in one read transaction. It
fails on missing, extra or corrupted files. Output contains integrity metadata,
not file contents. Authorized clients can read metadata through
`GET /projects/{id}/snapshots` and `GET /snapshots/{id}`. Lists accept offset and
limit (1–200). There is no HTTP endpoint accepting server filesystem paths and no
general-purpose file download endpoint. Worker delivery requires a live lease
and an explicit public release; see [input delivery](input-delivery.md).

Known credential filenames, symlinks and nonregular files are excluded; selected
root ancestors are opened without following symlinks. Exclusions are visible in
the manifest. Filename exclusions do not establish that captured data is free of
secrets or otherwise safe to share. Every captured file remains `unclassified` and
`imported_unverified`; checksum verification does not change those labels.
A separate [execution admission](execution-admission.md) workflow records explicit
public-data review and a release for one worker. It does not relabel the immutable
original manifest. Other classifications remain unimplemented. Verified worker staging, read-only container mounting, and bounded CPU supervisor
execution are implemented in native fixtures. Persistent output collection,
validation, and unattended research acceptance remain gated separately.

This is a content capture, not an atomic filesystem snapshot across all files or
reconstruction of historical research. The current manifest has the recorded
adoption timestamp but does not retain original source modification timestamps,
historical approvals, environment inventories or external job mappings. It cannot
establish when the original results were produced or whether they are correct.
