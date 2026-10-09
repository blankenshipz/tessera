# Bounded byte-verified tabular snapshots

The `bounded-tabular-regression-v1` profile parses actual UTF-8 CSV bytes, not
metadata-only MixFM records. It requires a reviewed byte hash/size, exact declared
schema, unique IDs, finite numeric features/target and nonempty fixed train,
validation and test labels. Bounds: 4MiB data, 10000 rows, 32 numeric features,
1024-byte cells, 64KiB observed manifest. It does not establish scientific validity.

`dataset_snapshot_models.py` defines closed immutable declarations and observed
manifests. `dataset_snapshots.py` provides `verify_dataset_bytes`,
`verify_dataset_file`, `manifest_bytes`, `build_dataset_capture`, and
`read_dataset_snapshot`, and `load_dataset_snapshot`. The loader returns the
observed manifest AND the same verified immutable bytes for starter consumption;
the workload must not reopen a mutable path after verifying it. Every observation
is recomputed from bytes. Declared
source/citation/license remain explicitly unverified origin/legal statements.
Expected hashes must be pinned by the separately authorized human review, not
treated as authority because an untrusted caller supplied matching metadata.

The retained snapshot contains exactly `dataset.csv` and
`dataset-manifest.json` (sorted-key compact JSON without final newline).
Each split digest hashes canonical JSON of its column names and original CSV
string rows in original order. The manifest digest is not the ordinary source
snapshot inventory digest; the approved execution plan must pin both layers.
Filesystem readers use anchored no-follow descriptors and reject symlinks,
unsafe paths, nonregular/oversized files and mutation during reading.

The normal upload/read API now reuses retained source snapshot adoption. Explicit
worker release, approved plan/grant, current lease/epoch input delivery and worker
materialization remain separate existing boundaries; there is no second blob store
or schema change. Upload adoption records one state/event/idempotency transaction;
replay rechecks current human/project scope and retained bytes. Reads reparse retained
CSV and canonical manifest against the retained inventory. No automatic grant,
classification, transfer consent or approval follows from CSV verification. Up to
four sequential variants require an already approved bounded human plan. Ray and
heavy orchestration/dependencies are not introduced.

## Actual API and operator sequence

Bootstrap a new authorized local development state with `tessera init --state-dir
"$STATE"`; `tessera serve --state-dir "$STATE" --port 8787` serves that state.
Use its existing private human `owner.header` (or an issued human-session credential)
through an authorized header-file reference. Never place credential values in request
bodies, logs or examples. Worker requests use a separately enrolled worker-audience
credential/header reference; a human/control token cannot call the worker route.
These are Tessera principals, not provider identity or research-harness publisher
enrollment. Bootstrap does not infer existing project/worker/grant approvals.

1. `POST /projects/{project_id}/tabular-dataset-snapshots`, human control audience,
   `Idempotency-Key` required. Body is `UploadDatasetSnapshot`: `declaration`
   (`version`, `profile`, typed `columns`, declared `source`), canonical
   `content_base64`, `expected_data_sha256`, `expected_data_size`,
   `expected_manifest_digest`, `expected_input_snapshot_digest`. Human review pins
   these targets; matching caller metadata alone is not a verdict. No server path,
   `verified`, owner, approval or schema-observation flags are accepted.
2. Response201 is the existing snapshot envelope (`id`, `project_id`, `digest`,
   inventory `manifest`, `actor_principal_id`, `created_at`,
   `transfer_authorized:false`) plus byte-derived `dataset_manifest` and its digest.
   `GET /tabular-dataset-snapshots/{snapshot_id}` returns this verified metadata for
   a currently eligible tenant/project control principal; it does not export raw CSV.
   External research agents must still use their curated context/disclosure paths.
3. Separately `POST /snapshots/{snapshot_id}/releases` as an eligible human with
   `expected_digest` equal to the **input snapshot inventory digest**, approved
   `worker_principal_id`, `classification:"public"`, explicit
   `permits_worker_transfer:true`, `rationale`, and aware `expires_at`. Verification
   and source/license declarations do not supply legal or transfer authorization.
4. Approve the bounded task plan pinning `execution_spec.input_snapshot_digest` and
   the manifest digest in the approved workload arguments. Human issue of
   `/projects/{project_id}/execution-grants` pins task revision/plan digest, worker,
   snapshot ID, image, exposure and expiry. Grant expiry must not exceed release
   expiry. No changed snapshot may substitute for the approved task's input digest.
5. Worker-audience `POST /worker/leases` with `grant_id`; then
   `POST /worker/leases/{lease_id}/inputs` with `expected_epoch` and idempotency key.
   Receipt binds lease/grant/snapshot/worker/epoch; `delivery_confirmed:false` does
   not claim transport receipt. Actual files are the normal envelope's base64
   `dataset.csv` and `dataset-manifest.json`, not private harness paper inputs.
6. Existing `materialize_snapshot` validates the envelope and writes read-only
   files. The workload uses `load_dataset_snapshot(...,expected_manifest_digest=...)`
   and consumes its exact returned bytes. Lease/release/grant/credential expiry,
   revocation, wrong worker/epoch, classification and retained corruption fail closed.

For reviewed request files and authorized header references (not actual credentials):

```sh
curl --fail --header @"$HUMAN_HEADER_REF" --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: reviewed-dataset-v1' --data-binary @upload.json \
  "$CONTROL_URL/projects/$PROJECT_ID/tabular-dataset-snapshots"
curl --fail --header @"$HUMAN_HEADER_REF" \
  "$CONTROL_URL/tabular-dataset-snapshots/$SNAPSHOT_ID"
curl --fail --header @"$HUMAN_HEADER_REF" --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: reviewed-dataset-release-v1' --data-binary @release.json \
  "$CONTROL_URL/snapshots/$SNAPSHOT_ID/releases"
curl --fail --header @"$WORKER_HEADER_REF" --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: dataset-delivery-v1' --data-binary @epoch.json \
  "$CONTROL_URL/worker/leases/$LEASE_ID/inputs"
```

Stable request/manifest/worker fields and exact integration ownership seams were
recorded as local evidence ID `DATASET-SNAPSHOT-INTERFACE-INTENT8`. Local tests exercise actual
temporary authenticated handlers, SQLite, release/grant/lease delivery and worker
materialization with synthetic principals/data. **They do not prove native Job
input mounting/execution, a deployed HTTPS flow, provider identity, scientific
validity, or human result review/export.** The assigned native worker owner must
complete that separate input-resource and ordinary Job journey.
