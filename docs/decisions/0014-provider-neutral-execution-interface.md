# 0014: Introduce a provider-neutral execution interface

Status: accepted; Kubernetes compatibility implementation landed.

## Decision

Define typed execution-plane contracts for an opaque `TargetBinding`, frozen
`ExecutionEnvelope`, attributed `OperationHandle`, and `Observation`. The
interface also declares capabilities and preflight plus independently composable
submit, observe, cancel, reconcile, collect, and account protocols.

An envelope preserves operation ID, package digest, target digest, lease epoch,
and absolute deadline. `unknown` is an observation rather than permission to
submit again. Cancellation confirmation and collection handoff remain distinct
from deletion acknowledgement, byte transfer, artifact acceptance, accounting,
and scientific result acceptance.

Kubernetes is the first compatibility adapter. Its translation layer delegates
to the existing Kubernetes execution adapter and journal; it does not replace
the intent-before-effect create fence, Job UID fencing, reconciliation, or
target-bound collection behavior. Kubernetes Job/Pod/PVC manifests, kubeconfig
and cluster validation, network policy evidence, and storage policy remain in
Kubernetes modules. The neutral payload is opaque to callers.

## Consequences

`provider.py` continues to define the separate fake development-provider seam
and is not generalized or activated by this decision. No Databricks, EC2, cloud
runtime, credential, or billing implementation is introduced. Future adapters
must implement the neutral contracts while retaining their own provider-specific
validation and durable side-effect journal.

See [DESIGN §3.1](../../DESIGN.md#31-configurable-execution-and-harness-contract)
and [decision 0010](0010-execution-adapter-composition.md).
