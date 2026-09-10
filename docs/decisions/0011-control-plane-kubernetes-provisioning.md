# 0011: Separate scoped control-plane provisioning

Status: accepted design direction; unimplemented.

## Decision

Control-plane provisioning is a workflow separate from research execution and
target attachment. A future scoped operator may reconcile only named resources in
an immutable provisioning plan after an eligible human approves that exact digest.
Research-plan approval, agent activity, worker enrollment, and skills do not grant
provisioning authority. Automatic destructive teardown is not authorized; deletion
requires a separate explicit human decision.

## Rationale and links

Separating operator authority prevents a workload request from becoming cluster
administration. This adds no operator, cluster call, or teardown implementation.
See [DESIGN §3.1](../../DESIGN.md#31-configurable-execution-and-harness-contract)
and [execution admission](../execution-admission.md).
