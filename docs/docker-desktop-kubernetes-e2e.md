# Docker Desktop Kubernetes conformance E2E

`scripts/check_docker_desktop_kubernetes_e2e.py` is the smallest standalone real,
provider-disabled Kubernetes conformance run. It accepts only an explicit
absolute kubeconfig and the `docker-desktop` context. It reads that selected
context's HTTPS server and CA identity, hashes the kubeconfig, and passes all
subsequent `kubectl` calls the explicit kubeconfig, context, server, and
namespace. It never uses a current-context fallback, Minikube, prune, broad
delete, or namespace teardown.

Run it only after the focused unit checks:

```sh
.venv/bin/pytest tests/test_kubernetes_provider.py tests/test_kubernetes_execution.py \
  tests/test_docker_desktop_kubernetes.py
.venv/bin/python scripts/check_docker_desktop_kubernetes_e2e.py --run \
   --kubeconfig "$HOME/.kube/config" --context docker-desktop
```

For the real authenticated control-plane dispatch path, run:

```sh
.venv/bin/pytest tests/test_kubernetes_dispatch.py tests/test_kubernetes_execution.py \
  tests/test_kubernetes_provider.py tests/test_kubernetes_control_binding.py
.venv/bin/python scripts/check_authenticated_kubernetes_dispatch_e2e.py --run \
  --kubeconfig "$HOME/.kube/config" --context docker-desktop
```

The authenticated runner starts a temporary loopback control service and uses
the existing authenticated local workspace-adoption command plus normal human
and worker HTTP routes to create a project, worker, input release, task plan,
grant, lease, admitted attempt, immutable package, and human-approved dispatch
outbox. The enrolled worker claims that outbox through the normal worker route;
the unchanged control-issued package is handed to the same nonblocking durable
adapter contract used by fake-callback tests; the runner never hides supervision
inside a blocking Job wait. It retains one uniquely named
namespace and its NetworkPolicy, PVC, Job/workload Pod and collector Pod. Its
temporary control state (and credentials) is removed after the result is printed;
its non-secret local adapter/claim journal is retained outside the repository.
The JSON evidence includes control IDs/package digest, target, resource UIDs,
image digest, exact output/hash, and experiment-export linkage.

The image at `examples/kubernetes-hello/` is built locally for `linux/amd64` as
an OCI archive. A per-run, non-secret image label prevents a retained node image
with a matching cached config from being selected for a new fixture. The runner verifies the archive/index manifest digest, config
digest, and image config architecture before loading exactly that archive into
the named Docker Desktop control-plane node's `k8s.io` containerd namespace. It
tags and verifies a canonical local tag and exact
`repository@sha256:manifest-digest`; the Job and collector both use that digest
reference with `imagePullPolicy: Never`. Build/load/digest
verification failures stop before namespace creation.

The run creates one unique `tessera-e2e-*` namespace, its deny-all
NetworkPolicy, a 64 MiB PVC, one non-root `batch/v1` Job, and one non-root,
read-only post-exit collector Pod. The Job writes exactly
`/outputs/result.txt` containing `hello tessera kubernetes e2e\n`. Before
collection, Kubernetes RBAC must explicitly allow `create pods`, `get pods`,
and `get pods/log`; the collector mounts the verified PVC read-only at both the
volume and mount layers and prints the bytes. Its observed PVC claim, read-only
mount, container security context, and Pod security fields must match the fixed
collector contract; the PVC UID/spec are rechecked before output logs are read.
Success requires the exact bytes, byte hash, Job/Pod/PVC/namespace UIDs, Job
spec/image checks, and collector image ID. The JSON result records these values.

The standalone adapter persists a local Job create/cancel fence before Kubernetes calls;
lost creates reconcile only by the deterministic Job name. Cancellation checks
the observed Job UID again before the narrowly named Job delete, then confirms
only fresh Job and owned-Pod absence. Kubernetes `kubectl delete` has no exposed
UID precondition, so a hostile same-namespace writer between that check and the
delete remains an enforcement limitation; the dedicated random namespace is the
boundary for this conformance fixture. The standalone run does not create
authority through SQL, fabricate a receipt, call OpenCode/providers, accept an
artifact or result, or claim pilot completion. The authenticated runner uses
normal control authority but remains a narrow, provider-disabled development
conformance fixture: it does not enable a supported Kubernetes target,
provider/model execution, artifact release, result acceptance, or pilot
completion. Both runners retain every created resource for manual inspection.
