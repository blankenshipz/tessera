# Artifact lineage and observed run receipts

Schema v58 adds two independent record sets: tenant-scoped artifact lineage
(`artifact_lineage_nodes`, `artifact_lineage_edges`) and a non-fake run receipt
class (`execution_run_receipts`, with its own result-acceptance ledger). Both are
installed empty by `migrate_v57_to_v58`; nothing is backfilled and no existing
fake receipt or acceptance is relabelled. Both are bookkeeping, not proof of
bytes, provider success or science.

## Purpose

Distillation produces a chain of artifacts: a teacher checkpoint, labels
generated from it, and a student checkpoint trained on those labels. Without a
recorded chain, a student result cannot be traced to the teacher bytes that
shaped it. Lineage records that chain as identified nodes (sha256 per project)
and typed, acyclic edges, attributed to the human who recorded them.

## Lineage nodes (`artifact_lineage_nodes`)

- `kind`: `model_checkpoint`, `dataset`, `labels`, `evaluation`, `other`.
- Identity: `sha256` (64 lowercase hex), unique per project
  (`UNIQUE(project_id, sha256)`). A second node with the same digest in the
  project is a `Conflict`.
- `origin`: `tessera_attempt` requires `producing_attempt_id`; `external_import`
  forbids it. Enforced in the request path and by table CHECKs.
- `model_checkpoint` nodes require `framework` and `format`. Checkpoint identity
  is therefore digest + producing attempt + framework/format.
- Optional bindings: `artifact_id`, `result_checkpoint_id`,
  `producing_attempt_id` (an admitted attempt), `operation_id` (external
  operation), plus `label` (1-200 chars) and `source_uri`.
- Same-project checks at registration: the artifact, result checkpoint, attempt
  (via its task) and operation must exist in the node's project (`NotFound`
  otherwise); `artifact.sha256` must equal the node `sha256` (`Conflict`); all
  bindings that name an operation must name the same one (`Conflict`).
- Registration requires the `human` role. Each node records
  `created_by_principal_id`; an event `human.artifact_lineage_node_registered` is
  written in the same transaction as the row and the idempotency record.

## Edges (`artifact_lineage_edges`)

An edge reads child -> parent ("child was derived from parent"). Fields:
`child_node_id`, `parent_node_id`, `relation`, `rationale` (1-4000 chars).

| relation | rule |
| --- | --- |
| `derived_from` | unrestricted |
| `trained_from` | child must be `model_checkpoint` |
| `labels_from` | child `labels` or `dataset`; parent `model_checkpoint` (teacher) |
| `evaluated_on` | child `evaluation` or `model_checkpoint` |

Violations return `Conflict`. Further rules:

- No self edge (rejected as an invalid request and by CHECK).
- Acyclicity across all relations: a recursive walk upward from the proposed
  parent must not reach the child. It runs inside the write transaction, so a
  concurrent writer cannot interleave a cycle.
- `UNIQUE(child_node_id, parent_node_id, relation)`; a duplicate is a `Conflict`.
- Trigger `artifact_lineage_edges_same_project` aborts an insert whose endpoints
  are not both nodes of the edge's own project and org.
- Both tables have `BEFORE UPDATE`/`BEFORE DELETE` triggers that abort
  (`artifact lineage records are append-only`). Mistakes are not edited; there is
  no retraction yet.
- Event `human.artifact_lineage_edge_recorded`, human role required.

## HTTP API

Base `/projects/{project_id}/artifact-lineage`. All routes take a control
bearer token; the project must be in the caller's org (and match the token's
project scope when it has one). POST routes require `Idempotency-Key`.

| Method | Path | Role | Purpose |
| --- | --- | --- | --- |
| POST | `/nodes` | human | register a node (201) |
| GET | `/nodes` | any authenticated principal in the project | page nodes; `offset`, `limit` (1-200, default 100), `kind` |
| GET | `/nodes/{node_id}` | same | one node |
| POST | `/edges` | human | record an edge (201) |
| GET | `/nodes/{node_id}/lineage` | same | `direction=ancestors\|descendants` (default ancestors), `max_depth` 1-64 (default 64); returns nodes with minimum `depth` and the edges among them |
| GET | `/export` | same | bounded project export |

Every returned node/edge/export carries
`authoritative_scope: "attributed_lineage_bookkeeping_only"`.

## Export

`GET .../export` returns `{version: 1, project_id, nodes, edges, digest,
authoritative_scope}`. Nodes and edges are ordered by `(created_at, id)`;
`digest` is the canonical digest of `{nodes, edges}`. The export is refused with
`Conflict` above 10,000 nodes or 50,000 edges.

`lineage_export_payload(connection, project_id)` exists for reuse, but the
experiment export (`export_experiment`) does not include lineage yet. That is a
request to the maintainers of the gate-evidence code in `experiments.py`, which owns that file.

## Receipt evidence classes

Before v58 the only receipt class was the fake-provider one. An operation now
carries at most one class.

| | fake | observed |
| --- | --- | --- |
| receipt table | `external_operation_receipts` | `execution_run_receipts` |
| `provider_kind` | `fake` (CHECK) | `local_docker_cpu`, `remote_kubernetes`, `remote_salad` (CHECK) |
| `receipt_evidence` | `fake_provider_development_only` | `observed_provider_execution` |
| acceptance ledger | `execution_result_acceptances` | `execution_observed_result_acceptances` |
| acceptance `evidence_label` | `development_synthetic_only` | `provider_observed_unassessed` |
| `provider_success` | `not_established` | `not_established` |
| `scientific_validity` | `not_assessed` | `not_assessed` |

The last two are CHECK-fixed in every table; no request can set them.

Mutual exclusion: trigger `execution_run_receipts_exclude_fake` aborts a run
receipt for an operation that has a fake receipt, and
`external_operation_receipts_exclude_run` aborts the reverse. The store also
checks and returns `Conflict` before the trigger fires.

### Run receipt (`execution_run_receipts`)

One row per operation (`operation_id` primary key), append-only
(`execution_run_receipts_append_only_update/delete`). Worker-supplied fields
(`ReportRunReceipt`): `lease_id`, `expected_epoch`, `provider_id`,
`command_digest`, `exit_code` (int32), `started_at`, `finished_at` (ISO-8601
with offset), `output_digest`, `receipt_payload` (object, canonical JSON at most
64 KiB), `receipt_digest`. The server requires `receipt_digest` to equal the
sha256 of the canonical payload, and `finished_at >= started_at`.

Server-derived fields: `org_id`, `project_id`, `task_id`, `grant_id`,
`lease_id`, `epoch`, `worker_principal_id` (from the operation), `adapter_version`,
`duration_ms`, `receipt_evidence`, and `provider_kind`. `provider_kind` is
looked up from the operation's `adapter_version` (`ADAPTER_PROVIDER_KINDS`); the
client cannot choose it. Only `docker-local-cpu-v1` -> `local_docker_cpu` is
mapped. Any other adapter gets `Conflict` ("No observed-provider receipt mapping
for this adapter"). `UNIQUE(provider_kind, adapter_version, provider_id)`.

Control verifies the receipt against its own records, never the worker's claims,
inside the same transaction as the insert (and again on every replay). Any
mismatch, or any record control lacks, is `409 Conflict` with the fixed message
"Run receipt does not match control's recorded operation." (no submitted value is
echoed):

1. `receipt_payload.operation_id` and `receipt_payload.plan_digest` must be present
   strings equal to the URL operation id and the grant's approved plan digest.
2. `command_digest` must equal sha256 of the compact, sorted-key, ASCII canonical
   JSON array of the argv in the approved task revision's execution spec (found
   through the grant, re-checking the approval and plan digest). The worker's argv
   is not trusted. The same encoding lives in `tessera_worker.run_receipt` and
   `execution_run_receipts.argv_digest`; a test pins both to shared vectors.
3. `output_digest` must equal `artifact_manifests.manifest_digest` for the
   operation. Exactly one manifest (joined to the operation's org) must exist, and
   its project, task, grant, lease, epoch, worker and plan digest must equal the
   operation's, grant's and lease's. The worker takes that digest from the
   manifest receipt control returned at upload (already recomputed by the transfer
   helper) and keeps its local seal digest in the payload as
   `sealed_manifest_digest`.

Checks mirror the fake receipt path: the operation belongs to the calling worker
and org, the lease id and epoch are current, the grant is unresolved and matches
the operation, `started_at` is not before the lease was issued, and
`finished_at` is not beyond the allowed future skew. A re-post with identical
identity fields returns 200 with the stored row; any difference is a `Conflict`.
It records event `worker.execution_run_receipt_recorded` and binds
`provider_id` on the operation if unset. It does not change observed state.

A nonzero `exit_code` is recorded as given. It is not interpreted as failure or
success.

| Method | Path | Role | Purpose |
| --- | --- | --- | --- |
| POST | `/worker/external-operations/{operation_id}/run-receipt` | worker token | report (201 new, 200 replay); `Idempotency-Key` required |
| GET | `/external-operations/{operation_id}/run-receipt` | control principal in the org/project | read; response adds `receipt_evidence`, `provider_success`, `scientific_validity`, `operational_only: true` |

## Result acceptance over observed receipts

`result_acceptances.py` chooses the ledger per checkpoint: if the checkpoint's
operation has a row in `execution_run_receipts`, acceptances go to
`execution_observed_result_acceptances`; otherwise to the fake ledger. The
fake ledger's CHECKs are unchanged. The observed ledger additionally stores
`provider_kind` and `exit_code`, and fixes `artifact_release = 'not_performed'`,
`evidence_label = 'provider_observed_unassessed'` and
`receipt_evidence = 'observed_provider_execution'`.

Before a human accept/reject, the receipt is re-verified against the current
checkpoint: payload digest recomputes, org/project/task/grant/lease/worker/epoch
match, the operation's `observed_state` is `exited`, and `receipt_digest` equals
the request's `expected_receipt_digest`. A mismatch is a `Conflict`. A nonzero
exit code does not block acceptance bookkeeping.

## What this does not establish

- No byte existence or validation: lineage nodes assert a digest; they do not
  check that stored bytes exist or hash to it (except equality with a bound
  `artifacts.sha256`).
- No provider success: `provider_success` is always `not_established`.
- No scientific validity: always `not_assessed`.
- No artifact release (`artifact_release` is `not_performed`).
- Emission only from the docker-local-cpu worker (open hook 1, below). No live
  Salad/remote result can reach either acceptance ledger: the fake receipt table remains CHECK-constrained to
  `fake`, and its only writer accepts only `fake`.
- Lineage is not generated from runs. Edges and nodes exist only when a human
  records them.

## Open hooks (owned by other code)

1. docker-local-cpu worker: implemented (`tessera_worker/run_receipt.py`,
   `DevelopmentCpuRuntime.post_run_receipt`, `DevelopmentWorkerService`). After a
   confirmed exit and a sealed, uploaded output, and only when no fake provider is
   configured and the adapter exposes docker inspect, the worker posts one receipt
   before it finishes (the grant reconcile stays a later control/human step).
   Control re-verifies every cited digest against its own records (see Run receipt). Facts: `provider_id` = container id; `exit_code`, `started_at`, `finished_at`
   from a terminal `docker inspect` that must agree with the confirmed observation
   (container id, labels, exit code); `command_digest` = sha256 of the canonical
   JSON array of the exact argv; `output_digest` = control's artifact manifest digest from the upload receipt (the local seal digest is `receipt_payload.sealed_manifest_digest`);
   `receipt_payload` = schema, adapter version, operation and plan digests, image
   digest and id, argv digest and count, network profile, termination confirmed.
   No host paths, environment, argv text or secrets. One attempt, with a
   deterministic `Idempotency-Key` (operation, lease, epoch); a restart re-derives
   the identical body and replays it. If the post fails or is refused, the worker
   reports `state: unknown`, `reason: run_receipt_failed` and a bounded detail,
   keeps the workspace, and claims neither `exited` nor quarantine. Not implemented
   for Kubernetes or the fake provider path.
2. Salad / remote (`remote_execution*.py`, `salad_*.py`): add the adapter ->
   `remote_salad` / `remote_kubernetes` mapping to `ADAPTER_PROVIDER_KINDS` and
   post the same fields from container-group terminal state. The table CHECK
   already admits those kinds.
3. Gate-evidence code: include `lineage_export_payload` in `export_experiment`.

## Worked example

Register teacher, labels and student (bodies for `POST .../nodes`):

```json
{"kind": "model_checkpoint", "sha256": "<64 hex, teacher>", "label": "teacher-v1",
 "origin": "external_import", "framework": "pytorch", "format": "safetensors",
 "source_uri": "https://example.invalid/teacher-v1"}
```

```json
{"kind": "labels", "sha256": "<64 hex, labels>", "label": "teacher-v1 soft labels",
 "origin": "tessera_attempt", "producing_attempt_id": "<admitted attempt id>",
 "operation_id": "<operation id>"}
```

```json
{"kind": "model_checkpoint", "sha256": "<64 hex, student>", "label": "student-v1",
 "origin": "tessera_attempt", "producing_attempt_id": "<admitted attempt id>",
 "framework": "pytorch", "format": "safetensors", "result_checkpoint_id": "<id>"}
```

Each response returns the node `id` (`lineage_node_...`). Then record edges
(bodies for `POST .../edges`):

```json
{"child_node_id": "<labels id>", "parent_node_id": "<teacher id>",
 "relation": "labels_from", "rationale": "Labels generated by running the teacher."}
```

```json
{"child_node_id": "<student id>", "parent_node_id": "<labels id>",
 "relation": "trained_from", "rationale": "Student trained on these labels."}
```

`GET .../nodes/<student id>/lineage?direction=ancestors` then returns the student
(depth 0), labels (1) and teacher (2) with both edges.
