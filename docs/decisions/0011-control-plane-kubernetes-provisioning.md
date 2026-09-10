# 0011: Separate scoped control-plane provisioning

Status: accepted design direction; narrow local-development bootstrap implemented.

## Decision

Control-plane provisioning is a workflow separate from research execution and
target attachment. A scoped operator may reconcile only named resources in
an immutable provisioning plan after an eligible human approves that exact digest.
Research-plan approval, agent activity, worker enrollment, and skills do not grant
provisioning authority. Automatic destructive teardown is not authorized; deletion
requires a separate explicit human decision.

## Rationale and links

Separating operator authority prevents a workload request from becoming cluster
administration. The implemented local operator is limited to the immutable
disposable `tessera-dev` Docker Minikube profile and its named `tessera-dev`
namespace and `tessera-output` PVC. It requires a human to supply the exact
displayed plan digest with an explicit apply flag, uses no ambient Kubernetes
context, and retains resources. It does not attach a target, dispatch work,
materialize a Secret, use credentials, or implement teardown. See
[the local provisioning note](../minikube-provisioning.md).
See [DESIGN §3.1](../../DESIGN.md#31-configurable-execution-and-harness-contract)
and [execution admission](../execution-admission.md).
