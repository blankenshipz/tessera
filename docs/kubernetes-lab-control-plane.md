# Kubernetes lab control plane

## Local composition safety checkpoint (2026-09-29)

The control, coordinator and dispatch-worker examples are **not** a deployable
composition on an empty Docker Desktop cluster. Check a candidate pair of
fresh, disposable names without creating or adopting anything:

```sh
.venv/bin/python scripts/check_local_kubernetes_composition.py \
  --kubeconfig "$HOME/.kube/config" --context docker-desktop \
  --control-namespace tessera-local-control-unique \
  --workload-namespace tessera-local-workloads-unique
```

The command intentionally exits nonzero with `blocked_no_effects`: it only
checks the selected local HTTPS/CA context, named namespace occupancy and
unresolved manifest values. It neither prints Secrets nor treats an absent
namespace as reserved. An existing namespace is never adopted or deleted.
This is a preflight, **not** the requested bootstrap or a live integration
result. The example manifests still need a source-built node-resident immutable
image reference, a unique owned pair of namespaces, an operator-owned TLS
gateway/CA, scoped project/worker credentials in Kubernetes Secrets, an exact
workload binding, fresh positive and negative connectivity observations, and
verified admission/storage isolation. Do not apply the examples or start the
credential-bearing worker until those prerequisites are implemented and
witnessed. Docker Desktop's presence of NetworkPolicy objects or a `kindnet`
DaemonSet is not enforcement proof. Local source-image restart and deterministic
Job fixtures below are independently useful but do **not** exercise this
deployed coordinator/worker composition.

An opt-in **read-only continuation gate** is available at
`scripts/gate_local_kubernetes_network.py`. It requires exact retained namespace
UIDs and ownership nonce, two separately provisioned running Python probe Pods
in `tessera-lab` (one labelled `tessera.io/tls-control-client=true`, one without
that label or control-hop), and the running `tessera-control-tls` gateway Pod.
Supply an absolute CA path inside the allowed probe Pod. Example (replace Pod
names and CA path with observed values; this command does not create Pods):

```sh
.venv/bin/python scripts/gate_local_kubernetes_network.py --run \
  --causal \
  --kubeconfig "$HOME/.kube/config" \
  --control-uid 84ffd885-ed3e-4cf9-8941-59a5123cb3e1 \
  --workload-uid 1869980f-998a-4ef9-a7ea-d500326150b5 \
  --nonce 0a05eff73e29eabd9e99e081412c52e8 \
  --allowed-pod OBSERVED_ALLOWED --denied-pod OBSERVED_DENIED \
  --gateway-pod OBSERVED_GATEWAY --ca-path /run/tessera-ca/ca.crt
```

The gate uses explicit-context bounded `kubectl get` and `exec`, compares Pod
and namespace identities before and after. With `--causal`, it brackets the
negative TCP observation with CA-verified HTTPS `/health/ready`, then creates
probe-only ingress/egress grants for the **same denied Pod and gateway IP/port**.
It requires HTTPS success under those grants, denial after UID-preconditioned
revocation, repeats the grant/revocation transition, and checks final allowed
HTTPS and identity restoration. A negative timeout is accepted only within
this controlled sequence; it alone proves nothing. Interrupted or ambiguous
cleanup requires named UID reconciliation before proceeding. It exits nonzero
on unknown or unexpected outcomes. Its output is a network/HTTPS observation,
**not** an authenticated API operation, worker credential, target binding,
policy enforcement certificate, or Job receipt. The denied client's own
functionality and expected policy selection must be independently validated
before asserting NetworkPolicy enforcement: TCP failure can have another cause.
No worker deployment, authority creation or live Job is implemented by this stage.

The analogous opt-in workload witness accepts `--causal` for temporary
probe-only grants to the same server Pod/IP/8080, twice testing connected under
the grants and denied after UID-preconditioned revocation. Existing workload
ResourceQuota must have room for its three probe Pods; do not delete older Pods
or raise a baseline quota merely to make this fixture pass.

This profile is a **single-active, provider-disabled lab deployment**. It gives
one control Pod durable SQLite state; it does not provide PostgreSQL, HA,
automatic target enrollment, provider calls, federation, or an externally
reachable production service. The separately opt-in fixture below joins it to
the existing deterministic Kubernetes dispatch seam; it does not promote that
seam to a supported execution target.

Do not grant pilot Job-create authority in this control namespace: denying reads
of its PVC does not prevent a Job mounting it. The constant-compute pilot requires
the separate [project workload namespace/admission profile](pilot-workload-isolation.md).
Existing synthetic same-namespace fixtures below are not a project isolation claim.

The [single-active lab operations runbook](single-active-lab-operations.md)
defines restart, PVC-loss, backup/held-restore, writer-fencing and RPO/RTO
procedures. It does not turn this profile into HA or authorize provider work.

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
`allow-labelled-control-clients`, `allow-labelled-control-client-egress`, and
`allow-tls-control-client-dns`. The duplicate control name is a Deployment and
Service. The PVC is `ReadWriteOnce` and the Deployment is `Recreate` with one
replica; do not scale it or attach its volume to another writer.

`/health/live` is a process liveness endpoint. `/health/ready` is a readiness
endpoint: it returns HTTP 503 for recovery hold, schema mismatch, or unavailable
state and exposes only profile, public origin, and an opaque persisted backend
identity. It does not authenticate a caller or disclose a bearer credential.
Readiness being true is not proof that TLS, NetworkPolicy enforcement, storage
durability, or research execution is working.

The default-deny policy blocks all ingress and egress. Only the operator-managed
gateway Pod labelled `tessera.io/control-hop: "true"` may reach the plaintext
control Pods on TCP/8765; that label must not be placed on credential-bearing
clients. The paired gateway egress permits DNS lookup via labelled `kube-system`
DNS Pods. Credential-bearing coordinator and dispatch Pods instead carry
`tessera.io/tls-control-client: "true"`, which permits DNS lookup but not a
plaintext control hop; their separate policies allow TCP/443 to the gateway.
No other application destination is opened by this profile.
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
approves a task, separately admits the baseline execution, runs one real
deterministic Job, and links its collected output hash as a quarantined artifact
measurement. It restarts the control Pod before worker reconciliation, then
interrupts a resumable output transfer with a second control-Pod restart and
requires the same upload progress and exactly one Job. The fixture records a
human `retain_quarantined` disposition and a private custody release, retrieves
the exact fixture bytes through the owner route, and exports a bounded bundle
whose released output is verified in a fresh temporary workspace. Input bytes,
skill material, and environment image material must remain explicit
not-exported/restricted/missing records. It has no provider/model calls and does
not print owner or worker credentials. The intervention is registered but not
executed by this single-Job fixture.

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
falls back to the current context. A pass is operational evidence only: private
custody release preserves quarantine/unclassified status and neither accepts an
artifact as a result nor establishes scientific validity.

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
