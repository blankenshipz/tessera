# Ordinary Kubernetes worker: installed output collection

This is the normal installed service, not a harness that uploads bytes on the
worker's behalf. The supported boundary is one explicitly bound Kubernetes target,
an independently running control service, and a durable worker workspace. The
owned Docker Desktop bench has live operational evidence; independent-host
availability, permanent ingress and scientific acceptance are separate gates.

## Installation and target prerequisites

Install the same trusted Tessera wheel on control/worker hosts and in a
digest-pinned collector image. See [installation](installation.md). Start the
ordinary process with `python -m tessera_worker.kubernetes_remote_service`, using
explicit control URL/CA, target file, Kubernetes CA/token, separate worker token,
and an absolute private durable `--workspace`. Do not mount control state into
the worker, copy an owner token into it, or use an ambient kube context.

The private target JSON requires the validated `cluster`, `output` and
`network_evidence` bindings, plus a top-level field:

```json
{"collector_image":"YOUR_INSTALLED_TESSERA_IMAGE@sha256:ACTUAL_64_HEX_DIGEST"}
```

This fragment is **not** a complete target or permission to dispatch. The collector
image must contain the installed `tessera_worker.kubernetes_output_pod` module.
The process rejects an absent/unpinned collector image; there is no image or
registry fallback. Explicit kubeconfig context/server/CA/digest, namespace UID,
output PVC UID and real allow/deny network witness must match the target.
The witness expires after **15 minutes**; credential renewal does not refresh it.
Do not edit old witness timestamps to pass admission.

Grant only the operator-reviewed workload-namespace permissions needed by the
service: Job get/create/delete; Pod get/list/create/delete for attributable helper
and workload observations; `pods/log` get for bounded helper output; and the exact
PVC/policy/namespace readbacks. Pod creation needs separately enforced admission
and namespace isolation; RBAC alone does not constrain Pod contents or selectors.
Preparation-recovery proof additionally needs complete execution-labelled Job/Pod
absence reads. A Job-list 403 makes that proof unavailable; it is not absence.
Do not widen permissions just to turn retained unknown history into success.
The opt-in [deployment examples](../deploy/kubernetes/README.md) are not safe to
apply unchanged and do not attest enforcement.

Budget namespace capacity for retained history **and** transient helpers. The
recorded baseline/intervention run with one retained old Job required capacity
for at least three Jobs and four Pods (old Pod, two workload Pods, one helper),
with helpers cleaned between phases. Other retained/probe Pods consume capacity
too. Object-count quota is not per-attempt cost, filesystem quota or network proof.

## Automatic custody, interruption and cleanup

The collector prepares a separate operation-scoped output directory before the
Job request, binds its marker to operation/attempt/lease/epoch/package/target/PVC,
then reads only declared regular output files. The service durably retains
immutable bytes and transfer intent before upload, placement and manifest work.
Artifacts stay quarantined; collection grants neither retrieval/disclosure nor
scientific acceptance. Lost acknowledgements resume the original idempotent
transfer rather than inventing a second artifact or provider receipt.

Keep the same exclusive workspace and all its journals after interruption.
Original Job and helper UIDs are durable identity fences, not values inferred
from a later object with the same name. Stop/restart must reconcile that original
Job; an unknown create reply is not permission to submit a replacement.
The known Kubernetes omission of a **false** `readOnly` default is accepted only
at the narrow validated fields; other isolation/spec changes remain rejected.

Transient preparation/collection helpers use UID-scoped cleanup. Preparation
cleanup requires its acknowledged owned result; collection cleanup follows
durable local byte custody. Missing original UID, changed spec/ownership,
unconfirmed termination or lost cleanup evidence remains unresolved. Cleanup
never deletes another writer's object or erases workload/operation history.

Custody and execution reporting are separate outboxes. Late post-fence bytes
remain quarantined late evidence. Retaining bytes does not complete an operation
descriptor: the ordinary worker must replay the relevant Running report and
receive an acknowledgement for its confirmed terminal observation. Human grant
reconciliation is separate from that report acknowledgement, and both remain
separate from an admitted-attempt/scientific outcome.

## Expiry and preparation recovery

The owner may renew the same **active** worker credential before expiry through
`POST /credentials/{id}/renew`; see [the exact limits](installation.md#credential-expiry-interruption-and-recovery).
Token/principal/journal identity stays unchanged. There is no unattended renewal,
revival of expired/revoked identities, grant extension or automatic worker-history
transfer. After expiry/revocation, retain original history and obtain fresh human
authority; a new worker cannot attest as the original worker.

The `pre_job_preparation` recovery path is distinct from zero-create
`bound_job_intent`/`pre_manifest` proofs. It requires the original active worker,
an explicitly stopped/exclusive original submitting service/workspace, immutable
original preparation Pod UID and digests, cancellation fences and fresh complete
absence readbacks. The original human grant creator separately reconciles the
attestation. See [decision 0028](decisions/0028-pre-job-preparation-recovery.md).
Missing original UID proof and scoped Job-list denials remain unavailable, not
backfilled from a current Pod or relabelled as never submitted. A succeeded
preparation helper is neither workload completion nor an effect-free operation.

## Observed live boundary (2026-10-04)

The protected ordinary-bench checkpoint records installed-image generation 4,
schema 50, 195 source files, one ready worker, and API HTTPS
connect/review/export/reconnect. It reuses generation-2 baseline **385 / 36 bytes**
and intervention **2870 / 37 bytes**, automatically retained with provenance and
quarantine, plus a stop/restart preserving the original Job UID. Generation 3
retained a **29-byte late** artifact including its trailing newline; generation 4 replayed
the pending Running and confirmed Exited acknowledgements and separately human-
reconciled the grant. Native review subsequently identified a **29-byte** late
artifact with SHA-256
`79e9e44fe379d350f50b6d52d47fb545c32137a775c063a89ba280d788f8b12c`.
Read-only reconciliation confirmed the same operation
`8530373cfa9c246927c740afad5bf409`, manifest
`artifact_manifest_48751aef7bfed4adf615a662e8a63225` and artifact
`artifact_cc8d0811e3d76b5e7bed0642c689a269` in original receipts and current bytes.
The earlier **28-byte summary omitted the trailing newline**; it remains preserved
as a corrected summary, not a different artifact or deleted history.
Scientific outcome remains **null**.

Earlier missing-original-UID proof and no-start Job-list 403 histories remain
unavailable/unknown. They were not erased by later successful attempts. The
control/TLS/worker run on owned Docker Desktop: this is laptop-local evidence,
not off-host availability. Normal installed client connectivity is
[`tessera connection-tunnel`](connection-tunnel.md), an explicit foreground
operator command for the existing TLS gateway, with namespace/Deployment UID and
public-CA references. It does not rely on the historical private route launcher
or deploy a gateway. The installed command's default owned selection passed real
authenticated connect/reconnect (receipt expiry 2026-10-04 09:54 UTC). This
supersedes, without erasing, the historical private route's 07:23 UTC deadline;
neither is permanent external ingress. See [the handoff](research-bench-handoff.md).
The
gateway's bounded thread-local TLS handshake repair passed concurrent reads and
same-Pod reconnect; incomplete handshakes are not an unbounded accept-loop hold.
Separate deployed-client receipts record two actual MCP SDKs reading released
36/37-byte artifacts, proposals/reconnect/expiry/revocation/destination denial and
draft-only human promotion, and the native TLS draft/revise/approve/output/export/
reconnect journey. Native output saves used a labelled fallback after the actual
OS picker invocation timed out at eight seconds without human selection: the
real selection step is **unverified**, not permission-denied. A separate native
health-refresh regression and 178 embedded-Node tests passed on that UI revision.
The subsequent production disposition facade/UI/IPC revision passed 192 Node
tests; four focused guard checks are separate evidence, not added to that total.
The new phased native plan/admit/dispatch-to-ordinary-worker/new-Job journey also
passed: task `task_e455b4713d9e4e15a6095367bac7d6ed` revision 3 / plan 2, explicit
human grant and worker package, ordinary Job UID
`e2c69a4b-ed1e-4ec0-9538-951f09f6e683`, automatic timely 385 / 36-byte output,
terminal acknowledgement and human accounting. Native `retain_quarantined` review
was explicit and preceded separate custody release, exact-byte save, reproduction
export and reconnect with unchanged attempt/operation IDs and no redispatch.
Saved SHA-256 is `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`;
scientific outcome remains null. Follow
[the explicit native/operator execution handoff](../desktop/EXECUTION.md) rather
to keep these actual phases distinct from proposal-only/history inspection or
fixtures. A backend `interrupted` label may map a reported exit: inspect actual
Job success, timeliness and separate human grant/scientific outcome, not the label
alone. Existing baseline/intervention outputs were timely (`late:false`) with
Succeeded Jobs and human grant reconciliation, not scientific acceptance.
No model-provider execution, paid inference or scientific validity was established.
