# Capability Kubernetes runner and installer — offline components

**Real activation remains blocked. No real Kubernetes Job, artifact download,
network probe, model/data fetch or cluster mutation ran for this implementation.**
Tests materialize only generated harmless wheels in temporary private directories;
they never execute/import those wheels. Passing this suite is not live conformance.

This adds a capability-specific profile, strict raw-observation inspector, durable
fake-client Job lifecycle and bounded installer logic. It does not enable
`GatedRealCapabilityEffects`, issue isolated control packages, or turn the
constant-pilot profile into installation authority.

## Components and authority

* `capability_runner_contract.py`: `RunnerPlan`, `ResourceBounds`, exact Job and
  operator prerequisite renderers, and `PackageVerifier`. The supervisor supplies
  an authenticated package-authority callback, its own tenant/project/worker
  identity, and the **already approved plan digest**. Decode/signature presence
  is not verification. The plan digest binds both project/package resource bounds.
  There is no default approved plan or isolated-package authority.
* `capability_inspection.py`: obtains a bounded bundle through an explicitly
  injected trusted collector, then checks raw object/status and challenged
  request/response shapes. Signed summaries are not accepted as raw proof.
* `capability_kubernetes.py`: `KubernetesCapabilityRunner`, `RunnerJournal`, and
  `FakeKubernetesCapabilityClient`. The runner currently accepts **only that exact
  fake-client type**. A real client cannot opt in by declaring enforcement flags
   or supplying a passing fake snapshot. The separate
   [authenticated evidence collector](capability-kubernetes-collector.md) now has
   an explicit credential loader and opt-in read/review/dry-run API client, not a
   real Job lifecycle client. No kubeconfig discovery or subprocess is supplied.
* `capability_installer.py`: hook-free materialization logic intended for a
  reviewed sandbox image. It accepts a verifier-issued package handle, approved
  plan, pinned lock bytes and supervisor-injected bounded transport. It neither
  chooses a network client nor launches a package manager.
* `capability_conformance.py` and the matching script: disabled-by-default offline
  schema/render command, with an explicit but still blocked `--live` boundary.

These are supervisor-owned interfaces, not agent-callable endpoints. The existing
authenticated control claim/revocation path remains authoritative. New additive
tables use the private, no-symlink, owner-locked, synchronous-FULL worker journal;
they do not migrate control state or reinterpret existing broker receipts.

## Manifest and quotas

The renderer produces Namespace, ServiceAccount, ResourceQuota, LimitRange,
NetworkPolicy, dispatcher Role/RoleBinding and separate Fail/Deny Job/Pod policies
and bindings. The runner **does not apply prerequisites** or provision PVCs. It
creates only its one suspended Job after inspection and global claim.

The Job has one immutable, already-node-present image and the exact capability
entrypoint/digest/nonce/operation arguments. UID/GID 65532, read-only root, dropped
capabilities, RuntimeDefault seccomp, no privilege escalation, no host namespaces,
no token automount and no service-link environment are required. Only `/workspace`
and `/outputs` mounts are allowed. No init/sidecar/ephemeral containers, projected
credentials, Secret/hostPath mounts, arbitrary commands, environment injection or
image pulls are admitted. Full nonce/package/plan bindings are annotations; hashed
labels bind tenant/project/operation/package/nonce within Kubernetes length limits.
Cleanup checks the **full nonce and UID**, not truncated labels.

CPU, memory and ephemeral-storage requests equal limits and LimitRange defaults.
ResourceQuota permits at most one Job/Pod and one already-provisioned output PVC,
with aggregate compute/storage limits. The workload has no RBAC bindings, and
dispatcher permissions cannot create extra PVCs or other resource types. Effective
reviews and forbidden-resource negative tests are required for both identities.
Quotas must be at or below **both** approved bound sets and the signed artifact/
declaration byte/time budgets. Unknown enforcement mechanisms are rejected.

**Object presence is not hard enforcement.** There is no standard Kubernetes Pod
`resources.limits.pids`, so no fictional PID resource is rendered. PIDs require an
observed cgroup-v2 bound. The inspector also requires CPU throttle, memory OOM,
PID max and time-termination observations, plus byte/inode `EDQUOT` observations
on the exact output/ephemeral filesystems. Kubelet eviction, PVC size, emptyDir
sizeLimit or ResourceQuota presence alone cannot pass.

The installer checks allocations before each write and checks authority/deadlines
throughout. Its counters constrain **this writer**, not a different process or a
stuck kernel call. Independent project quotas and watchdog preemption remain
mandatory deployment gates.

## Installer and artifact custody

Only an approved `package` action using the existing pip/uv declaration format is
supported. Neither executable is invoked. The wheel must be in the approved
registry/channel/destination, with pinned URI/hash/size/content type. The pinned
lock must enumerate exactly that single artifact. No shell, dependency resolution,
build hooks, model or dataset download path is added.

Transfers use bounded chunks, declared-size-plus-one detection, SHA-256, repeated
authority checks and a total cooperative deadline. The existing pinned-IP HTTPS
primitive can be composed **inside a future sandbox**, not as host broker effects.
Default tests use in-memory streams; no ambient proxy/CA/cloud credentials are read.

Supported wheels are pure-Python `py3-none-any`, with one matching metadata set,
no dependencies and a complete hash/size-verified RECORD. Source/native packages,
entry-point scripts, `.data` relocation, `.pth`, links, executable-mode entries,
duplicate/traversing paths and over-budget expansion are rejected. No downloaded
code is imported or executed.

Staging uses private directory FDs, exclusive/no-follow files and the journal's
lifetime owner lock. `install-<full nonce>.partial` is verified by re-reading the
actual files, fsynced, then atomically renamed without replacing a prior install.
Its `tree/` represents quarantined materialization of the approved logical root,
**not installation into host site-packages**. The receipt target digest binds the
package and logical root. A future image must map this deployment-owned custody
root onto the declared sandbox output mount; no host installation route is enabled.

Partial bytes remain quarantined. Failure is `failed`; publication/rename reply
loss or restart of a started install is `unknown`. Neither permits retry. Receipts
retain approved identity, actual hash, inventory digest/file count and hash-chain
linkage—not source bytes, upstream errors/headers, signatures or untrusted archive
filenames. Local materialization is not human release or scientific acceptance.

## Evidence and lifecycle

The journal burns operation/package/plan/nonce before inspection. Failed evidence
cannot be repaired by reusing the attempt. Its digest binds operation, package,
namespace UID, plan and the raw bundle. Observations must be collected during this
inspection, at most five seconds old and within activation expiry. Missing or
contradictory objects/status/events, policy type warnings, quota usage, identity
changes, stale/future/replayed challenges and signed-only claims fail closed.

Checks include Job **and Pod** positive/negative admission; dispatcher and workload
self-identity/access reviews and attributed negative tests; quota/LimitRange;
PVC/PV ownership; allowed challenges and policy-UID drop observations for forbidden
network paths; TLS/IP/certificate/issuer/redirect behavior; and independent
watchdog identity, heartbeat and broker-loss/revocation termination observations.
A timeout is never proof of a network denial or stopped workload.

Lifecycle:

1. Durable inspection and global-claim intent; fresh evidence and current authority.
2. Durable create intent, **suspended** Job creation and exact UID binding.
3. Arm the exact-UID watchdog, inspect its sequential heartbeat, recheck authority,
   persist start intent and unsuspend only once.
4. Monitor ownership, actual image ID, readiness, heartbeat and authority. Normal
   termination requires consistent Job **and** nonempty owned-Pod terminal status.
   Exit zero alone is not installation success.
5. Cancellation records `cancel_requested`; delete acknowledgement is not stop.
   Fresh exact-UID/full-nonce Job and owned-Pod absence confirms `terminated`.
6. Lost replies/crashes remain `unknown`; reconciliation never creates/resumes or
   retries. Cleanup deletes only the exact owned Job, never PVCs/journals/artifacts.

The bundle format is strict and fake-tested, **not a general Kubernetes REST
decoder**. API-version defaulting, full cluster RBAC enumeration, CSI/node identity
provenance, raw CNI/kernel collection and observer independence still require
reviewed live integration. The authenticated collector retains raw API evidence
but does not implement a production runtime witness. Structured fixtures cannot establish these
facts. The closed real-client boundary prevents that gap from enabling effects.

## Later Docker Desktop conformance

```sh
# Disabled; no config, credentials, API calls or effects:
.venv/bin/python scripts/check_capability_kubernetes_conformance.py

# Authoritative strict versioned config schema:
.venv/bin/python scripts/check_capability_kubernetes_conformance.py --schema

# Offline render only; does not verify signatures or apply resources:
.venv/bin/python scripts/check_capability_kubernetes_conformance.py --render \
  --config /absolute/private/capability-conformance.json

# Currently exits 2 before opening config or credentials:
.venv/bin/python scripts/check_capability_kubernetes_conformance.py --live \
  --config /absolute/private/capability-conformance.json
```

`DockerDesktopCapabilityConformance.model_json_schema()` requires literal boolean
opt-in, `docker-desktop`, loopback HTTPS API, CA digest, separate opaque dispatcher/
observer credential references, the harmless wheel fixture, complete RunnerPlan,
at most 128 requests/120 seconds and zero retries. Unknown fields are rejected.
These are future harness bounds, not evidence that a live harness ran.

Before any separately authorized harmless live conformance run, supply/review:

* Authenticated isolated-package issuance/global claim/reconciliation, approved
  plan digest and independently approved project/package quota bounds.
* A reviewed image containing the entrypoint and installer, bounded authenticated
  package/lock delivery and explicit output-root mapping. The reserved executable
  name is not proof that such a built image exists.
* Explicit local target/CA/namespace/SA/PVC/CSI/node identities. No current-context
  fallback, exec credential plugins, cloud credentials or shared control PVCs.
* Live conformance of the authenticated API collector, independent runtime witnesses,
  retained raw artifacts and reviewed API defaults/CEL compilation/admission.
  These policies have **not** been compiled by a Kubernetes API server here.
* Actual filesystem quotas, cgroup/PID configuration, CNI enforcement and external
  watchdog daemon/transport with hard preemption evidence on the exact target.
* Authenticated transfer into the existing broker/control custody transaction.
  New component receipts are not automatically valid control receipts.

Do not weaken policy, delete unknown Jobs/PVCs, or re-sign historical observations
to make gates pass. No live conformance was attempted: these prerequisites are not
established. Existing provider-disabled defaults remain blocked.

## Regression scope

`tests/test_capability_kubernetes_runner.py` and `tests/test_capability_installer.py`
use only a fake API, synthetic observations, temporary journals and generated
wheels. Existing activation/TLS/watchdog, broker/journal, custody and
provider-disabled regressions remain separate evidence. Exact command results
are in the task handback; no full integration or live enforcement is implied.
