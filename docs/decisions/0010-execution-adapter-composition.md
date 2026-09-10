# 0010: Compose explicit environment and execution adapters

Status: accepted design direction; unimplemented.

## Decision

Keep `local-cpu` as the first execution target while Kubernetes preparation runs
in parallel. Future profiles compose an `EnvironmentAdapter` (explicit target
identity, preflight, bootstrap/attachment boundary, and credential delivery) and
an `ExecutionAdapter` (submit, observe, logs, cancel, accounting). Both preserve
the immutable package, operation identity, and lease epoch.

Kubernetes attachment must name and validate kubeconfig/context/server/CA plus
observed cluster and namespace identities. Ambient current-context selection is
forbidden. Minikube bootstrap is separate from target attachment.

## Rationale and links

This keeps local CPU progress independent of cluster readiness and makes target
authority auditable. It does not implement an adapter or activate Kubernetes.
See [DESIGN §3.1](../../DESIGN.md#31-configurable-execution-and-harness-contract),
[decision 0009](0009-minikube-job-conformance.md), and the
[compatibility matrix](../compatibility-matrix.md).
