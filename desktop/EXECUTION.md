# Native plan-to-execution handoff

The native client owns human plan review, experiment design, admission and
target-bound dispatch. Operator configuration and worker-audience preparation
use the normal authenticated API. They are explicit steps, not hidden fixture
effects. Never place owner/worker tokens in renderer fields or receipt JSON.
Use private credential-file references in a main-process/operator client.

## Required fresh operator facts

Before creating execution authority, provide the following **metadata only**:

- Route origin, public CA file reference, route owner, current readiness witness
  and expiry. Desktop consumes the route; it never replaces its processes.
- Project ID, retained snapshot ID, canonical manifest digest, reviewed input
  contents/relative paths, classification and destination-specific release ID,
  worker principal ID and release expiry. A placeholder digest is not input.
- Enrolled worker readiness/heartbeat and credential expiry (credential **path
  reference**, never value), supported target digest, profile version
  `development-minikube-opencode-v1`, pinned image repository plus SHA-256.
  Include namespace/PVC UIDs and the fresh real allow/deny network witness
  timestamp/expiry; that witness expires after 15 minutes independently of tokens.
- A bounded argv using actual retained input paths, exact output paths, expected
  output byte/hash or other operational validation, duration and output cap.
  Profile is `kubernetes-opencode`, network is false, pinned skills are empty
  unless the installed worker explicitly supports their verified materialization.
- Current project execution-limit revision, currency, available exposure and
  concurrency; intended grant exposure/expiry and explicit acknowledgement that
  infrastructure cost is unmeasured. Zero recorded exposure is not a hard cost cap.
- Installed ordinary worker's collector image, exact PVC UID/spec/output binding,
  provenance fences, output caps, durable quarantine and readiness for automatic
  lease renewal, dispatch claim, observations and output collection are mandatory.
  Confirm how it coordinates the operator-prepared lease/package; do not create a
  second lease, extend the hard deadline or bypass worker ownership.
  A `persistent_output_storage` descriptor is **optional** on this Kubernetes
  collector path. If absent/null, omit **both** `persistent_output_storage` and
  `persistent_output_collection` from the package request. Installed Kubernetes
  automatic collection is independent of that opt-in storage-capability contract.
  This omission does not attest noexec/nosuid/nodev or storage-slot conformance.
  If supplied, all descriptor guarantees must actually be verified; set
  `persistent_output_collection: true` only with that valid descriptor.
  Descriptor fields are `contract_version: 1`, `allocation_id`, `filesystem_id`,
  `mount_id`, `capacity_bytes`, `capacity_inodes`, `write_uid: 65532`, and literal
  true `noexec`, `nosuid`, `nodev`, `write_access_verified`,
  `supervisor_custody_verified`, `no_nested_mounts_verified`. Supply verified
  storage facts, not fabricated booleans. Capacity must cover declared outputs.

Do not reuse the historical proposal-only local CPU plan with digest `a*64`,
empty output paths and a one-byte output limit.

## Screen and API sequence

1. **Connect → project → New proposal → Revise this proposal.** Enter the real
   snapshot digest and strict bounded execution specification above. Review and
   explicitly approve the exact current task revision/plan digest. Read them back.
2. **Operator human API boundary.** Review/release the snapshot with
   `POST /snapshots/{id}/releases` (`expected_digest`, `worker_principal_id`,
   `classification: public`, `permits_worker_transfer: true`, rationale, expiry).
   Configure limits if required with `PUT /projects/{id}/execution-limits`
   (expected revision, currency, monetary limit cents, max concurrency).
   Issue `POST /projects/{id}/execution-grants` with task ID, current task
   revision, exact plan digest, worker principal, snapshot ID, pinned image,
   expiry, currency, max exposure cents and
   `acknowledges_unmeasured_infrastructure: true`. Read back the grant.
3. **New experiment.** Register the real task ID and plan digest on a variant;
   retain a baseline/intervention design and honest method/input provenance.
   **Review separate execution admission → Record admission** with that grant
   ID. Read back the returned admitted attempt ID and current revision.
4. **Worker API boundary**, using the enrolled worker credential, not the human
   credential: `POST /worker/leases` with `grant_id`. Then
   `POST /worker/leases/{lease_id}/package` with `expected_epoch`,
   `execution_adapter: kubernetes-job`, and `admitted_attempt_id`. Normally omit
   both optional storage fields for the supported installed Kubernetes collector
   path. Only include `persistent_output_collection: true` together with an
   actually verified `persistent_output_storage` descriptor. The control rejects
   true collection without a descriptor, or a descriptor without the true flag.
   Preserve stable command keys and
   inspect uncertain replies before retrying. A lease is short-lived; coordinate
   renewal and dispatch promptly with the installed worker rather than pausing
   this boundary for manual screenshot work.
5. **Refresh experiment → Review Kubernetes dispatch → Authorize dispatch.**
   Review the actual package digest/revision and enter the approved target digest.
   The installed worker, not the desktop harness, creates/supervises the Job.
6. **Refresh status, remote workflow and artifact inventory.** Inspect the original
   Job UID, attributed running/exit observations, termination and automatic output
   provenance. `interrupted` can be the control mapping of a reported exit, not
   evidence that compute failed. Inspect provider completion, lateness and human
   outcome separately; leave scientific validity unassessed.
7. **Review/export → disconnect/reconnect.** Quarantined bytes are not readable
   merely because compute completed. A human must review and explicitly release
   exact artifact custody before native preview/save or reproduction-bundle export.
   Verify saved bytes/hash against the retained manifest, then reconnect and confirm
   the same durable attempt/operation/output IDs without replaying commands.

Every mutation needs a stable `Idempotency-Key`. No grant, lease, dispatch,
custody release or scientific approval is implicit. Consult
[admission](../docs/execution-admission.md), [runtime](../docs/cpu-runtime.md) and
[ordinary collection](../docs/ordinary-worker-collection.md) for backend contracts.

## Acceptance evidence

Keep proposal-only/history review receipts separate from **new** execution
evidence. A completed path requires a fresh readiness witness, real approved plan,
grant/admitted attempt/package/dispatch linkage, ordinary worker-created Job UID,
automatic retained outputs and native review/export/reconnect. DOM assertions,
Electron screenshots, OS-picker selection and provider observations are distinct.
The existing actual picker timeout is not a permission denial or a passing picker
test. A labelled file-selection fallback cannot complete that OS boundary.

## Opt-in native phased harness

`test/electron-bench-execution.cjs` is prepared for the reviewed retained
snapshot and `/hello --cpu-sum-squares 10 /outputs/result.txt` workload (expected
operational value 385, 36 output bytes). It is distinct from the old
proposal-only/history harness. Consult separate phase receipts for actual live
coverage; completed plan/dispatch/inspection cannot establish export acceptance.

After the integration owner has supplied fresh metadata, launch:

```sh
NODE_EXTRA_CA_CERTS=/absolute/public-ca.crt env -u ELECTRON_RUN_AS_NODE \
  node_modules/.bin/electron test/electron-bench-execution.cjs \
  /absolute/private-client-references.json plan /absolute/private-execution-receipts
```

The referenced metadata requires `origin`, `ca_file`, `owner_credential_file`,
`route_service_reference`, `execution_ready: true`, `ready_expires_at`,
`witness_expires_at`, `worker_expires_at`, `release_expires_at`, `project_id`,
`snapshot_id`, `snapshot_digest`, `snapshot_release_id`, `worker_principal_id`,
`target_digest`, and `pinned_workload_image`. Use current verified values, not
the expired release/witness. The CA must be loaded before Electron launch.
Optional `private_runtime_dir` must be an owned non-symlink directory outside all
receipt trees, normally `.tessera/private/native-execution/<run>`. If omitted,
the harness derives a run directory under the repository's `.tessera/private`.
Derived header and Electron auth/profile state live only there (0700 directory,
0600 header), never under receipt output. Receipts contain filepath references
only. Do not copy that private directory into receipt bundles or manifests.

`plan` writes `execution-state.json` with native-approved task ID, task revision,
plan digest and exact spec. It also registers the experiment through the native
design form and returns `experiment_id` and baseline `variant_id`. Baseline10
binds the approved task/digest; intervention20 is explicitly only a future
procedure with no task approval, execution or claimed result. The phase exits
without a grant. The human operator issues the normal grant from those facts.
Add the returned `experiment_id`, `variant_id` and explicit operator `grant_id`
to metadata and run the same command with phase `admit`.

`admit` commits only native admission, returns `admitted_attempt_id`, and exits.
The enrolled worker client acquires/prepares the normal lease/package with that
attempt ID. The optional storage descriptor/flag must both be omitted unless its
full guarantees are actually verified; collector/PVC/provenance/output controls
remain mandatory regardless. Add `admitted_attempt_id` and the
actual `package_digest`; run phase `dispatch` promptly while lease authority is
live. It commits native dispatch, **not** a claimed Job or successful compute.

For the normal optional-descriptor path the exact worker package body is:

```json
{"expected_epoch":1,"execution_adapter":"kubernetes-job","admitted_attempt_id":"ACTUAL_NATIVE_ATTEMPT_ID"}
```

Use the actual lease epoch, not this example's `1`. Neither storage field is sent.
`operatorPackageRequest` in `test/bench-execution-contract.cjs` prepares this
metadata body for an omitted/null descriptor and rejects unsupported supplied
guarantees. It does not make an API call or independently verify mount facts.
The native receipt separately states that storage-capability conformance is not
asserted. Actual automatic output custody is checked only after the Job and
retained manifest/bytes exist.

After the installed worker completes automatic collection, run phase `inspect`.
Supply `job_evidence_reference`, an integration-owned metadata-only readback of
the **actual new Job**, with these fields:

```json
{
  "operation_id": "ACTUAL_NATIVE_DISPATCH_OPERATION",
  "package_digest": "ACTUAL_PREPARED_PACKAGE_DIGEST",
  "admitted_attempt_id": "ACTUAL_NATIVE_ADMITTED_ATTEMPT",
  "target_digest": "ACTUAL_APPROVED_TARGET_DIGEST",
  "new_job": true,
  "job_uid": "ACTUAL_KUBERNETES_UID",
  "provider_state": "Complete",
  "created_at": "ACTUAL_JOB_CREATION_ISO_TIMESTAMP",
  "observed_at": "ACTUAL_FRESH_PROVIDER_READBACK_ISO_TIMESTAMP"
}
```

These are readback facts, not permission to manufacture evidence. The harness
matches them to native dispatch, requires an attributed terminal worker
observation and checks the automatically retained timely quarantined output
metadata for exact expected size/hash. Only then can `newExecutionJobs` be true.
It returns manifest ID/digest and artifact ID/bytes/hash without reading bytes.

For phase `export`, supply explicit `custody_review` metadata with
`human_approved: true`, that exact `manifest_id`, `expected_manifest_digest`,
current custody `expected_revision` and human `rationale`. The native custody
form records researcher retrieval release; it does not classify or accept science.
Before that separate custody step, supply `disposition_review` with
`human_approved: true`, exact `manifest_id`, `expected_manifest_digest`, current
**disposition** `expected_revision`, and human `rationale`. The new native
**Review quarantine retention → Load exact output metadata** control shows the
selected project's manifest/output IDs, bytes, hashes, quarantine/classification
and lateness, then explicitly records only `retain_quarantined`. It does not
release bytes or accept results. Disposition and custody revisions are separate.
The backend retains its human-role/tenant/digest/revision/authorization guards;
the desktop bridge exposes no arbitrary endpoint, rejection/classification,
publication or agent-disclosure action. A recorded human review is not proof of
scientific validity. Inspect/export do not require a new execution witness or
input release after compute ends; current authenticated route/owner access and
exact record bases still apply. Never refresh stale timestamps by editing them.
The harness then uses native preview/save, verifies exact expected36bytes,
exports the reproduction-bundle record and disconnects/reconnects with unchanged
attempt/operation/manifest IDs and counts. Inspect screenshots separately; a DOM
assertion is not proof that its pixels or the OS picker were observed.

The harness never invokes the workload itself, impersonates worker reports,
uploads output or creates grants/leases/packages. It preserves stable mutation
keys and inspects committed plan basis on continuation. Stop/inspect uncertain
effects before retrying. Receipts are saved separately as
`execution-{phase}-receipt.json` alongside cumulative `execution-state.json`.
Syntax/contract tests are not live execution proof. A prior custody/export 409 is
retained as failure history; a new explicit disposition review does not by itself
prove custody release, save/export or reconnect. File selection is
explicitly labelled fallback, not OS-picker acceptance. The derived private header
path remains in the receipt for human selection and must be removed after use.
