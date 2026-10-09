# Docker Desktop pilot workload isolation

**Offline implementation only. No resources were applied, credentials issued,
live probes run or pilot Jobs started by this change.** This is a narrow
constant-workload profile, not general hostile-code multi-tenancy.

## Inspected target

The retained `.tessera/comparison-pilot-20260921-local/target-provisioning/RUN-RECORD.md`
reports a Job-creating account and output PVC in `tessera-local-control-20260921`.
Denying GET of the control PVC does not prevent a Job mounting it: the kubelet,
not the submitting account, attaches the claim. Namespace labels are not RBAC.
The account also lacked GET of the two namespace UIDs used as cluster identity.

That record reports unexpectedly reachable forbidden probe paths; later
control-path timeouts do not erase them. NetworkPolicy enforcement, hard output
quota and cost enforcement remain unverified. Recorded grants expired at 20:47 UTC
and the worker credential/input release at 20:57 UTC on September 21, 2026.
Neither variant had started in that record. We neither refreshed those authorities
nor re-inspected the live cluster or modified its private records.

## Deployment boundary

`PilotIsolation` renders 13 Kubernetes objects from one non-secret profile:

* A deterministic `tessera-exec-<24 hex>` namespace, hashing **both** organization
  and project. It must differ from the declared control namespace. Use a **new**
  namespace; never adopt a populated namespace or move control into it.
* Restricted Pod Security pinned to v1.30, and two v1 ValidatingAdmissionPolicies
  with Deny bindings. They match the namespace **name**, not caller-supplied
  workload labels. One checks Jobs; the other checks Pods and ephemeral-container
  updates. Separate kinds keep CEL type checking precise. Missing fields and
  evaluation errors fail closed; there is no Ignore/Warn fallback.
* Only the approved digest-pinned image and `python -c 'print(1)'` / `print(2)`.
  No image pulls, sidecars, init/ephemeral containers, Secret/environment injection,
  alternate ServiceAccounts, token automount/projection, host namespaces/hostPath
  or privilege. Only memory-backed `/work` and `pilot-output` at `/outputs` may
  be mounted. The default workload account has no bindings; `pilot-dispatcher`
  is never selected by Pods and its credential stays outside the namespace.
* Fixed 500m CPU / 256Mi memory / 64Mi ephemeral-storage per container, one
  container, no retries, at most 120 seconds, parallelism/completions one. Quota
  limits two retained Jobs, two active Pods, one 64Mi-request PVC and aggregate
  requests/limits. **ResourceQuota is not a filesystem quota or billing cap.**
* `pilot-dispatcher`: create/get/delete Jobs, read/list Pods and read logs, GET
  only the named output PVC, default-deny policy and quota. No Pod create/exec,
  Deployment/CronJob, Secret, token issuance, PVC/RBAC/policy mutation or
  cross-namespace workload authority. Job create/delete cannot be label-scoped
  with RBAC; only this project lives here.
* A named ClusterRole/Binding: GET only namespaces `kube-system` and the execution
  namespace (no list/watch), GET the two named policies and bindings, and create
  SelfSubjectReview/SelfSubjectAccessReview. Reviews confer no ability to review
  another subject or administer the cluster.

Kubernetes >=1.30 with ValidatingAdmissionPolicy, PodSecurity and ResourceQuota
admission enabled is required. Administrators must not exempt this namespace or
disable those plugins. Before submission the pilot verifies namespace UIDs/labels,
exact policy/binding/quota specs, policy observed generation and warning-free type
checking. Additional broad RoleBindings, webhook mutations, namespace/node/cluster
administrators remain trusted. The bounded permission probe is not an exhaustive
audit of every resource, verb, namespace or future binding.

Existing admitted-package digest, tenant/project, operation, lease/epoch, expiry
and cancellation checks remain. Admission does **not** authenticate Tessera grants
from Job annotations. A stolen dispatcher token could repeatedly delete/create
allowed constant Jobs in its own namespace during its lifetime; quota is not a
lifetime submission budget. It cannot mint control authority. Keep the trusted
dispatcher and short-lived credential discipline.

## Offline rendering and separately authorized installation

Copy/edit `deploy/kubernetes/pilot-isolation.example.json` with actual org/project,
control namespace and approved already-node-present image. The example digest is
illustrative, not a runnable image.

```sh
# JSON Kubernetes List to stdout; no credential read, API call or file write.
.venv/bin/python scripts/check_pilot_isolation.py --render \
  --config /absolute/private/isolation-profile.json

# Default does nothing, even if a config path is supplied.
.venv/bin/python scripts/check_pilot_isolation.py
```

The generated List is the deployment manifest and shares its source with runtime
readback; no handwritten policy copy can drift. Retain/review the exact bytes
before a **separately authorized** operator installation. Policies/bindings precede
namespace/RBAC entries. Verify API support and policy readiness before issuing any
token; partial apply is not readiness. No automatic apply, install, image pull,
retry, token generation or teardown exists. `standard` storage is specific to
Docker Desktop and does not imply hard byte/inode enforcement.

An authorized operator must separately retire the old shared-control-namespace
worker binding/credentials: the new namespace does not revoke old authority.
Preserve control/output PVCs and audit records; do not migrate/delete storage to
make this check pass. Confirm the new namespace has only intended resources and
no broad inherited grants. Only then obtain an authorized short-lived dispatcher
token, retained as a private bearer-only file outside Git and outside workload
Pods. This task does not perform or authorize those installation effects.

## Opt-in local authorization/admission probe (not run here)

Probe config has exactly `target` and `runtime`. `target` uses the pilot's typed
`cluster`, `output`, `credential_scope` plus `isolation` (below). Use observed
namespace/PVC UIDs and spec digests, pinned CA and exact existing kubeconfig digest.
The probe may use `network_none_evidence: null`: it constructs no network
attestation. Authentication uses only the explicit ServiceAccount token, never
kubeconfig credentials or an exec plugin. Runtime has exactly:

```json
{
  "ca_file": "/private/pilot/cluster-ca.pem",
  "compute_credential_file": "/private/pilot/dispatcher.token"
}
```

Add this **required** `target.isolation` to both probe and full pilot configs:

```json
{
  "control_namespace": "tessera-local-control-20260921",
  "image": "REPLACE_WITH_APPROVED_DIGEST_PINNED_IMAGE"
}
```

Use the derived execution namespace, `credential_scope.service_account` of
`pilot-dispatcher`, `output.pvc_name` of `pilot-output`, `/outputs`, and new observed
identities. The approved package image must match the policy image. Old shared
namespace configs are rejected before compute credential reads. Existing grants
and target/cost/storage/network evidence are not silently rebound to new targets.

```sh
# Only after installation and explicit permission for local API probes:
.venv/bin/python scripts/check_pilot_isolation.py --probe \
  --config /absolute/private/authority-probe.json
```

The probe requires literal loopback HTTPS and context `docker-desktop`, disables
proxies/redirects, pins CA/server/kubeconfig and verifies both namespace UIDs and
the actual SelfSubjectReview identity. Bounds: **64 requests**, **120 seconds**
between calls, **five-second deadline per call** (a final in-flight call can add
five seconds), bounded responses, **no retries**. Only GETs, self-reviews and Job
POSTs with `dryRun=All` are possible. No real create/delete, Pod exec, PVC write,
image pull, control API call or cloud credentials. No pilot journal is opened.

It checks 32 effective permissions, two positive Job shapes and ten negatives:
control PVC, hostPath, projected token, dispatcher account, automount, Secret
environment, sidecar, changed command, privilege and host networking. Negatives
need the selected policy denial marker: a generic RBAC/quota/schema rejection is
not a pass. Unavailable/changed resources, type warnings or unexpected allows
block. Retain the report (non-secret identity/check metadata and request/response
digests) outside Git; raw credentials and upstream diagnostics are never printed.

A pass does not establish actual scheduling/controller-created Pod admission,
NetworkPolicy enforcement, storage durability/quotas or provider cost.
`network_enforcement` is always `unverified`. The constant-only admission policy
does not authorize arbitrary network canaries: later connectivity testing needs
a separately reviewed operator procedure, not a worker exemption or fabricated
evidence. Execution's existing fresh `network_mode=none` evidence gate remains
closed. Generic synthetic adapter/conformance fixtures are unchanged; only this
pilot's live transport requires the new deployment contract.

## Offline verification for this change

On the existing concurrently edited tree, this targeted command passed **117
tests** (two existing Starlette/httpx and anyio deprecation warnings):

```sh
.venv/bin/pytest -q tests/test_pilot_isolation.py tests/test_pilot_authority_transport.py tests/test_comparison_pilot.py tests/test_comparison_pilot_remote.py tests/test_kubernetes_contracts.py tests/test_kubernetes_execution.py tests/test_kubernetes_control_binding.py tests/test_credential_access_leases.py
```

Scoped Ruff lint/format checks and `git diff --check` passed. A larger command also
including `test_kubernetes_dispatch.py` and `test_kubernetes_dispatch_recovery.py`
was stopped by the shell's 120-second timeout, then its bounded 300-second retry
also timed out without a pytest summary. Neither is counted as a completed gate.
Full integration acceptance, Kubernetes CEL compilation/admission, actual Pod
scheduling and local network probes remain unrun. No dependency installation,
cloud credentials, live pilot, commit or push was performed by this task.
