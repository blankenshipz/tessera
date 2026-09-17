# Kubernetes lab control plane

This profile is a **single-active, provider-disabled lab deployment**. It gives
one control Pod durable SQLite state; it does not provide PostgreSQL, HA,
automatic target enrollment, provider calls, federation, or an externally
reachable production service. The separately opt-in fixture below joins it to
the existing deterministic Kubernetes dispatch seam; it does not promote that
seam to a supported execution target.

Docker Compose explicitly builds the `dev` target for development and tests.
Build the hardened `lab` target explicitly and replace both `image` values in
`deploy/kubernetes/lab-control-plane.yaml` with the resulting immutable image
reference. Before applying, set the ConfigMap's trusted host and HTTPS public
origin to the exact hostname served by an operator-managed TLS/private-network
endpoint. The public-origin hostname must occur in `TESSERA_TRUSTED_HOST`.
Do not use `*`, an HTTP origin, a path, or an ambient host assumption.

```sh
docker build --target lab --tag registry.example/tessera-control:LAB_IMAGE .
kubectl --kubeconfig /absolute/kubeconfig --context EXPLICIT_CONTEXT apply -f deploy/kubernetes/lab-control-plane.yaml
kubectl --kubeconfig /absolute/kubeconfig --context EXPLICIT_CONTEXT -n tessera-lab get pvc,deploy,service,networkpolicy
```

The manifest creates `tessera-lab`, `tessera-control-state`,
`tessera-control`, `tessera-control`, `default-deny`, and
`allow-labelled-control-clients`. The duplicate control name is a Deployment and
Service. The PVC is `ReadWriteOnce` and the Deployment is `Recreate` with one
replica; do not scale it or attach its volume to another writer.

`/health/live` is a process liveness endpoint. `/health/ready` is a readiness
endpoint: it returns HTTP 503 for recovery hold, schema mismatch, or unavailable
state and exposes only profile, public origin, and an opaque persisted backend
identity. It does not authenticate a caller or disclose a bearer credential.
Readiness being true is not proof that TLS, NetworkPolicy enforcement, storage
durability, or research execution is working.

The default-deny policy blocks all ingress and egress. Only Pods in the same
namespace labelled `tessera.io/control-client: "true"` can call the Service;
the paired labelled-client egress policy permits only TCP/8765 to the selected
control Pods plus TCP/UDP 53 to labelled `kube-system` DNS Pods for Service-name
lookup. No other application destination is opened by this profile.
NetworkPolicy enforcement, PVC backing/durability, and the selected storage
class's single-writer behavior remain cluster capabilities the operator must
verify. The manifest intentionally supplies no Ingress or TLS certificate.

Use the opt-in Docker Desktop restart fixture only with a new,
fixture-owned namespace:

```sh
.venv/bin/python scripts/check_kubernetes_control_plane_restart.py --run \
  --kubeconfig "$HOME/.kube/config" --context docker-desktop \
  --namespace tessera-control-e2e-YYYYMMDD
```

It renders the shipped profile into the new fixture namespace, replacing only
fixture ownership, local image, PVC size, and localhost remote-binding values.
It builds the local fixture image with `docker buildx build --load`, saves one
bounded local archive, and imports it into Docker Desktop's `kind` node
`k8s.io` containerd namespace before it creates only the supplied new namespace
and its labelled resources, writes a synthetic project through a local
port-forward, restarts the fixture Pod, and compares the project, its single
`project.created` event cursor, idempotent replay response, and backend identity.
Cleanup is bounded and reported; it verifies both the fixture label and the
namespace UID observed after apply, retaining rather than deleting a replacement.
Docker Desktop's `kind`
cluster cannot use Docker's legacy image store: enable **Use containerd for
pulling and storing images** before running the fixture. The fixture verifies
Docker Desktop is running in `kind` mode, imports the archive through the named
`desktop-control-plane` node's `ctr` interface, does not push to a registry,
and does not retry with a different image source. On an availability timeout it reports
the Pod status and up to ten Pod events, but never container logs or control state.
Confirm the image store before the live run; it must report the containerd
snapshotter rather than `overlay2`:

```sh
docker info -f '{{ .DriverStatus }}'
# [[driver-type io.containerd.snapshotter.v1]]
```

## Persistent-control experiment fixture

After focused fake checks, this retained live fixture renders the shipped profile
into a **new fixture-owned namespace**, registers baseline and intervention,
approves a task, separately admits execution, runs one real deterministic Job,
links the collected output hash as a quarantined artifact measurement, restarts
the control Pod, and reopens the worker journal to require the same operation
and exactly one Job. It has no provider/model calls and does not print owner or
worker credentials.

```sh
.venv/bin/pytest tests/test_kubernetes_lab_experiment_e2e_runner.py \
  tests/test_kubernetes_experiment_execution_slice.py \
  tests/test_kubernetes_control_plane_restart_runner.py
.venv/bin/python scripts/check_kubernetes_lab_experiment_e2e.py --run \
  --kubeconfig "$HOME/.kube/config" --context docker-desktop \
  --namespace tessera-lab-experiment-e2e-YYYYMMDD
```

The command refuses an existing namespace and uses the explicit kubeconfig and
`docker-desktop` context for every Kubernetes call. It builds/imports only the
fixture control and hello images into Docker Desktop's node containerd store,
then retains the namespace, PVCs, Job, Pods, and non-secret adapter journal for
inspection. It never deletes the namespace, uses a registry, reads a Secret, or
falls back to the current context. A pass is operational evidence only: it does
not release an artifact or accept a scientific result.

## Retained image-load fixture record (2026-09-17)

Read-only inspection was performed against the explicitly selected Docker
Desktop `docker-desktop` context before cleanup. No container logs, Secret
objects, ConfigMap data, PVC contents, or control-plane state were read. The
only ConfigMap in each namespace was the cluster-provided `kube-root-ca.crt`;
only its name, metadata, and data-key name were inspected. No harness final JSON
or other per-run metadata was retained in these namespaces.

All four namespaces below had the required
`tessera.io/control-restart-fixture` label, a single `control` Deployment,
`state` PVC, and `control` Service. Every `state` PVC was `Bound` (64 MiB,
`ReadWriteOnce`, `standard`); the Services were ClusterIP on port 8765. Their
fixture-owned labels establish only cleanup ownership, not a successful fixture
verdict.

| Attempt / namespace | Fixture label | Observed result before cleanup | Exact recorded cause / qualification |
| --- | --- | --- | --- |
| `imageload` | `63412d88abdf` | Deployment `0/1` available; Pod `control-df97d74b9-t4plj` Pending, init container waiting with `ErrImageNeverPull`. | The node did not contain `docker.io/library/tessera-control-restart:63412d88abdf` while `imagePullPolicy: Never` was required. The Deployment recorded `ProgressDeadlineExceeded` and `MinimumReplicasUnavailable`. This is the failed image-visibility attempt. |
| `imageload-v2` | `67facdbdf4f3` | Deployment `1/1` available; Pod `control-76cff9f88-qtx8b` Running/Ready, no restarts. | The node reported the image already present. One startup readiness probe saw `connect: connection refused` at 02:37:19Z, before the Deployment became available at 02:37:29Z. No retained final harness verdict establishes whether the complete restart/persistence checks passed. |
| `imageload-v3` | `44cdfa49f9e8` | Deployment `1/1` available; Pod `control-6c7f5855-w64f4` Running/Ready, no restarts. | The node reported the image already present. One startup readiness probe saw `connect: connection refused` at 02:39:27Z, before availability at 02:39:37Z. No retained final harness verdict establishes whether the complete restart/persistence checks passed. |
| `imageload-v4` | `1217812fe190` | Deployment `1/1` available; replacement Pod `control-d8d9b864f-s5v7b` Running/Ready, no restarts. | The first Pod (`control-d8d9b864f-x7s8d`) was stopped and replaced; both startup instances recorded a transient readiness `connect: connection refused` event before the replacement became available at 02:41:52Z. No retained final harness verdict establishes whether the complete restart/persistence checks passed. |

This record does not replace, reinterpret, or delete the separately retained
successful v6 result. In particular, this cleanup is limited to the four exact
`imageload` namespaces listed above; the other labelled retained namespace
(`tessera-control-e2e-20260917final` at inspection time) is out of scope.
