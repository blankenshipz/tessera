# Minikube/OpenCode development conformance

`tessera_worker.kubernetes_contracts` is a pure Python contract for a proposed
local Minikube Job shape. It does **not** start, inspect, configure, or mutate
Minikube. It uses no Kubernetes SDK and has no provider, credential, OpenCode,
control-plane, desktop, or network path.

## Contract

A caller must explicitly provide the `development-minikube-opencode-v1` profile,
an absolute kubeconfig path and its SHA-256, context, HTTPS API server and its
SHA-256, CA SHA-256, namespace, and observed cluster/namespace UIDs. There is
no default namespace, current-context fallback, environment lookup, or kubeconfig
read.

The canonical `batch/v1` Job has a deterministic operation-derived name and a
digest-pinned image. It carries Tessera operation, attempt, lease, epoch, and
package identity in labels/annotations, freezes a canonical spec digest, uses
one bounded non-root container, has resource requests equal to limits, and sets
an active deadline. It forbids restart retries, privilege escalation,
capabilities, service-account token automounting, host networking/PID/IPC, host
mounts, and a writable root filesystem.

Observed Jobs and owned Pods must reproduce that identity and template before
they are attributed. Provider identity is a deterministic digest of the observed
cluster UID, namespace UID, and Job UID. Malformed, unknown, foreign, or changed
observations remain unknown or fail closed; they never create a replacement Job.

The local create-intent journal exists only to test lost-reply behavior. A caller
persists intent before invoking its injected create callback; retry observes the
same deterministic Job name rather than calling create again. Delete completion
is never inferred from an acknowledgement: fresh injected reads must show both
the Job and all owned Pods absent.

## Running the local unit contract

No Minikube setup is required or permitted for these tests:

```sh
.venv/bin/pytest tests/test_kubernetes_contracts.py
```

The tests use dictionaries and injected local callbacks only. They check identity,
digest/replay/duplicate fences, mismatch and malformed observations, cancellation
absence semantics, and the fixed security/resource fields.

## Non-goals

This is **development conformance only**. It is not enrollment, storage quota,
provider dispatch, a real OpenCode integration, artifact release, accounting,
result acceptance, or pilot acceptance. See [decision 0009](decisions/0009-minikube-job-conformance.md)
and the OpenCode activation gates in the [compatibility matrix](compatibility-matrix.md).

It is also not Minikube bootstrap, target attachment, control-plane provisioning,
or credential delivery. Those planned workflows require separately approved,
immutable plans and explicit target identity; skills cannot approve them or use
raw `kubectl` or cloud credentials.
