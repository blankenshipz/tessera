# 0009: Explicit Minikube Job conformance contract

Status: accepted for development conformance only.

## Decision

The worker-side `tessera_worker.kubernetes_contracts` module defines a narrow,
dependency-free Kubernetes `batch/v1` Job contract for the explicitly named
`development-minikube-opencode-v1` profile. It accepts an explicit kubeconfig
path and digest, context, HTTPS server and digest, CA digest, namespace, and
cluster/namespace UIDs. It never selects a current context, reads a kubeconfig,
or instantiates a Kubernetes client.

The builder accepts only digest-pinned images and fixed bounded command,
deadline, CPU, memory, and ephemeral-storage inputs. It derives one canonical
Job name and immutable operation/attempt/lease/epoch/package labels and
annotations. Its single-container template is non-root, read-only-root,
non-privileged, capability-dropped, token-disabled, host-namespace-disabled,
and has no host mounts. `backoffLimit` is zero and `restartPolicy` is `Never`.

The contract parses only Jobs and owned Pods that reproduce the frozen identity
and immutable template. It derives the provider identity from the observed
cluster, namespace, and Job UIDs. A private local intent journal fences a
create before the caller invokes its explicitly injected create function. Lost
create replies reconcile by the deterministic name and never create a second
Job. Binding, manifest, Job UID, and provider identity conflicts fail closed.

Cancellation invokes only an explicitly injected delete function. Its
acknowledgement is unresolved until fresh injected Job and owned-Pod queries
both report absence. A terminal Job or Pod is not substituted for that absence.

## Consequences and limits

This is development conformance evidence, not Minikube enrollment, cluster
provisioning, storage-quota validation, provider dispatch, OpenCode activation,
or pilot acceptance. It adds no control-plane schema/API, desktop route,
provider credential, Kubernetes SDK, ambient configuration, or live-cluster
call. The local journal is only a worker-side test seam; a future dispatch path
must bind it to admitted authority and a reviewed durable operation journal.

Before any real profile is activated, Tessera still needs selected target and
version pins, node enrollment/build and isolation evidence, credential and cost
reconciliation, storage/output gates, real cancellation/recovery tests, and all
applicable DESIGN.md acceptance gates.
