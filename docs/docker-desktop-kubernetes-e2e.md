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
  --kubeconfig "$HOME/.kube/config" --context docker-desktop \
  --namespace "tessera-e2e-auth-dispatch-$(date +%s)" \
  --image 'docker.io/library/tessera-e2e-hello@sha256:EXACT_NODE_MANIFEST'
```

The authenticated runner starts a temporary loopback control service and uses
the existing authenticated local workspace-adoption command plus normal human
and worker HTTP routes to create a project, worker, input release, task plan,
grant, lease, admitted attempt, immutable package, and human-approved dispatch
outbox. The enrolled worker claims that outbox through the normal worker route;
the unchanged control-issued package is handed to the same nonblocking durable
adapter contract used by fake-callback tests; the runner never hides supervision
inside a blocking Job wait. It creates one uniquely named, fixture-labeled
namespace and fixture-labeled NetworkPolicy, PVC, Job/workload Pod and collector
Pod. Its temporary control state (and credentials) is removed after the result is
printed; its non-secret local adapter/claim journal is retained outside the
repository.
The runner checks the pinned kubeconfig bytes before each kubectl request,
requires the named Docker Desktop node in the explicitly selected cluster before
creating a namespace, refuses an existing namespace, and rechecks cluster and
namespace UIDs before workload creation. Success-path cleanup requests deletion
with a Kubernetes namespace UID precondition after verifying fixture ownership;
`deletion_requested` is an API acknowledgement, **not** proof that namespace
and Pods are absent. Failed/recovery runs retain their fixture for explicit
reconciliation instead of deleting uncertain running work. A failure during
namespace construction may leave partial resources. No Kubernetes Secrets are
inventoried or accessed by this fixture.
The JSON evidence includes control IDs/package digest, target, resource UIDs,
image digest, exact output/hash, experiment-export linkage, and cleanup status.

The authenticated runner accepts `--mode success` (the default), `--mode
cancel`, `--mode restart`, and `--mode lost-report`. Recovery modes use the
normal authenticated dispatch claim/package/adapter path. They require an exact
already node-resident image supplied with `--image`; this runner never builds,
loads, pulls, tags, or otherwise changes an image. The source's deterministic
`/hello --sleep-write SECONDS /outputs/result.txt` mode is required for the
three recovery modes. If the supplied retained image lacks that mode, is missing,
or cannot remain running long enough, the runner prints `state=unavailable` and
stops without a success claim. `--timeout` is bounded to 180 seconds and every
live wait is bounded; `--workload-seconds` is bounded to 120 seconds and must be
less than the timeout.

`cancel` first observes the actual Job running, then uses the ordinary
authenticated grant-revocation route. The restarted supervisor sees the control
fence, invokes the existing narrowly named Job cancellation adapter, and emits
the durable cancellation proof. The JSON includes the original Job UID,
post-cancel Job/owned-Pod presence, report states, and either fresh
Job/Pod-absence confirmation or explicit unresolved termination. It rejects a
replacement UID. `restart` exits one supervisor after submission, opens a new
supervisor over the same local journal, and requires it to observe the same
still-running Job UID without a create. `lost-report` discards exactly one
response only after the normal authenticated `/worker/observations` request has
returned to the test transport; restart must replay the persisted payload and
idempotency key exactly before submitting a fresh terminal observation. Its JSON
includes report states, IDs, output, and output hash.

The standalone runner's image at `examples/kubernetes-hello/` is built locally for `linux/amd64` as
an OCI archive. A per-run, non-secret image label prevents a retained node image
with a matching cached config from being selected for a new fixture. The runner verifies the archive/index manifest digest, config
digest, and image config architecture before loading exactly that archive into
the named Docker Desktop control-plane node's `k8s.io` containerd namespace. It
tags and verifies a canonical local tag and exact
`repository@sha256:manifest-digest`; the Job and collector both use that digest
reference with `imagePullPolicy: Never`. Build/load/digest
verification failures stop before namespace creation.

The run creates one unique `tessera-e2e-*` namespace, a declared deny-all
NetworkPolicy, a 64 MiB dedicated-output PVC (with Tessera ownership/role
labels), one non-root `batch/v1` Job, and one non-root,
read-only post-exit collector Pod. The Job writes exactly
`/outputs/result.txt` containing `hello tessera kubernetes e2e\n`. Before
collection, Kubernetes RBAC must explicitly allow `create pods`, `get pods`,
and `get pods/log`; the collector mounts the verified PVC read-only at both the
volume and mount layers and prints the bytes. Its observed PVC claim, read-only
mount, container security context, and Pod security fields must match the fixed
collector contract; the PVC UID/spec are rechecked before output logs are read.
Success requires the exact bytes, byte hash, Job/Pod/PVC/namespace UIDs, Job
spec/image checks, and collector image ID. The JSON result records these values.

The PVC's 64 MiB request and the fixture's exact byte check are not a hard
Docker Desktop write/inode quota. The adapter rechecks dedicated PVC labels and
identity before collection, while the collector's bounded stdout only proves the
declared two-byte artifact bound. Docker Desktop local-path byte and inode
enforcement, as well as provider billing, remain unknown; this conformance run
must not be used as pilot storage or cost evidence.

The declared Docker Desktop policy is **not** evidence that a CNI enforced it.
This runner does not claim `network-policy-enforced-v1` or `network_mode=none`:
its workload uses `network_mode=cluster`. Use the separate opt-in Astra
conformance probe to record both bounded allowed and denied connectivity before
recording an enforced capability for its exact cluster and namespace.

The standalone adapter persists a local Job create/cancel fence before Kubernetes calls;
lost creates reconcile only by the deterministic Job name. Cancellation uses a
Kubernetes `DeleteOptions.preconditions.uid` request for the observed Job UID,
rather than a check-then-name-delete, then confirms only fresh Job and owned-Pod
absence. Kubernetes `kubectl delete` has no exposed UID precondition; a
replacement is rejected by the API and remains unresolved. The dedicated random
namespace remains a fixture boundary, not a general tenant-isolation guarantee.
The standalone run does not create
authority through SQL, fabricate a receipt, call OpenCode/providers, accept an
artifact or result, or claim pilot completion. The authenticated runner uses
normal control authority but remains a narrow, provider-disabled development
conformance fixture: it does not enable a supported Kubernetes target,
provider/model execution, artifact release, result acceptance, or pilot
completion. The authenticated runner verifies its exact namespace UID and unique
fixture ownership labels, then inventories workload resources before deletion.
It refuses cleanup on a UID/label mismatch or an unowned resource, preserving the
namespace for reconciliation rather than deleting a replacement or unknown work.
Inventory covers `all,networkpolicies,persistentvolumeclaims`, not every possible
namespaced API type; it cannot prove arbitrary namespace ownership.

Pass `--namespace UNIQUE_FIXTURE_NAMESPACE` to make the live fixture name visible
in its command and output. The name must not already exist: the runner creates it
with a fresh ownership label and will not adopt an existing namespace.
