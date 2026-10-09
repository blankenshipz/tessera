# 0017 — Separate pilot workload authority from control storage

Status: implemented offline; not applied or verified on a live target.

DESIGN's explicit-target and separate provisioning/credential boundaries apply.
The retained 2026-09-21 Docker Desktop target record exposed a real RBAC gap:
denying GET of the control PVC does not deny a Job template that mounts it.
Project labels and a separate ServiceAccount in the same namespace do not fix it.

The constant-compute pilot now requires a new namespace derived from the exact
organization/project pair, separate from control. Add restricted Pod Security,
fail-closed Kubernetes v1 ValidatingAdmissionPolicies for both Job templates and
Pods, a resource quota, default-deny NetworkPolicy, and narrowly scoped RBAC.
The allowed workload is deliberately only the two constant commands in the
operator-approved digest-pinned image. Only memory-backed work and the project's
output PVC may be mounted. Tokens, Secret references, sidecars and host access
are not workload capabilities. The dispatch credential remains outside Pods.

An operator-installed ClusterRole permits GET of **only** the selected execution
namespace and `kube-system`, plus the named admission policies/bindings. This is
the cluster-identity verification path, not a grant of cluster administration or
namespace listing. Self-subject review endpoints verify the actual runtime token.

The pilot re-reads exact policy/binding/quota specifications and namespace identity
before submission. The existing admitted-package, tenant/project, lease/epoch,
expiry, cancellation and provider-disabled contracts remain authoritative. A
policy cannot authenticate a Tessera grant from caller-supplied Job annotations;
the trusted dispatcher is still required. It can create/delete bounded allowed
Jobs in its own project namespace, not arbitrary cross-project resources.

Offline rendering and opt-in **server-side dry-run** authorization probes are
separate from apply, credential issuance and live execution. No inference of
NetworkPolicy enforcement, hard filesystem quota, provider cost, or pilot success
is allowed. See [the operator contract](../pilot-workload-isolation.md).
