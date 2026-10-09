# Astra operator Kubernetes setup

`tessera astra-setup` is an operator-only, plan-driven preparation boundary for
one retained Docker Desktop Kubernetes profile: `docker-desktop-kubernetes-v1`
under setup contract `astra-operator-setup-v3`.
It is separate from Minikube bootstrap, target attachment, worker enrollment,
research plans/grants, dispatch, credentials, Secrets, and provider activity.

Every live command requires an absolute kubeconfig and the explicit
`docker-desktop` context. It reads the selected context's HTTPS API server, CA
digest, kubeconfig digest, and cluster UID; it never uses current-context
fallback. The profile pins a namespace, an exact `repository@sha256:...` image
identity, and a runtime identity even though setup does not start that image.

The only apply allowlist is a named retained Namespace, deny-all NetworkPolicy,
64 MiB dedicated-output PVC, and an exact ResourceQuota. The PVC and the other
retained resources carry `app.kubernetes.io/managed-by=tessera`,
`tessera.io/output-role=dedicated-pilot-output`, and an operator-setup owner
label. The ResourceQuota admission-enforces at most one claim and a total
`requests.storage=64Mi` in that dedicated namespace. It uses bounded, explicit `kubectl get`/`create -f -` calls and
rejects a differing pre-existing spec or changed cluster identity. It does not
accept generic manifests, Helm, SSH, cloud, install, reset, public exposure,
Secrets, provider configuration, workload creation, delete, prune, or cleanup.
It records whether each resource was created or pre-existing plus observed UID
and spec digest in an append-only fsync'd private journal. Status reconciles
against those apply-time UIDs: a same-named replacement is reported as
`replaced` and requires reconciliation rather than being adopted. For a bound
PVC, Kubernetes-assigned `spec.volumeName` is binding state and is ignored;
the approved access mode, storage request, StorageClass, and volume mode remain
validated.

List profiles without touching the host:

```sh
.venv/bin/tessera astra-setup profiles
```

Use an existing private directory outside the repository for outputs and the
journal. First validate or discover the explicit target, then render the exact
plan. `--image` must be a real pinned identity selected by the operator; setup
does not pull or verify it.

```sh
PRIVATE_DIR=/absolute/private/operator-directory
IMAGE='docker.io/library/tessera-e2e-hello@sha256:EXACT_64_HEX_MANIFEST'
.venv/bin/tessera astra-setup validate \
  --target docker-desktop-kubernetes-v1 --kubeconfig "$HOME/.kube/config" \
  --context docker-desktop --namespace astra-conformance-20260914 \
  --image "$IMAGE" --runtime-identity provider-disabled-kubernetes-v1
.venv/bin/tessera astra-setup discover \
  --target docker-desktop-kubernetes-v1 --kubeconfig "$HOME/.kube/config" \
  --context docker-desktop --namespace astra-conformance-20260914 \
  --image "$IMAGE" --runtime-identity provider-disabled-kubernetes-v1 \
  --output "$PRIVATE_DIR/discovery.json"
.venv/bin/tessera astra-setup plan \
  --target docker-desktop-kubernetes-v1 --kubeconfig "$HOME/.kube/config" \
  --context docker-desktop --namespace astra-conformance-20260914 \
  --image "$IMAGE" --runtime-identity provider-disabled-kubernetes-v1 \
  --output "$PRIVATE_DIR/plan.json"
```

The operator must inspect the emitted plan digest and explicitly supply it to
apply. A plan is stale if the kubeconfig digest, CA identity, or cluster UID
changes. The journal is mandatory before any create; its parent must already be
an operator-owned private directory.

```sh
.venv/bin/tessera astra-setup apply --plan "$PRIVATE_DIR/plan.json" \
  --approved-plan PLAN_DIGEST --journal "$PRIVATE_DIR/setup.journal"
.venv/bin/tessera astra-setup status --plan "$PRIVATE_DIR/plan.json" \
  --journal "$PRIVATE_DIR/setup.journal"
```

`ready` means only that the named resource identities, owner labels, and declared
specs were observed. The run record distinguishes the enforceable Kubernetes API
admission bound (`persistentvolumeclaims=1`, `requests.storage=64Mi`) from
runtime output storage: byte and inode write bounds are `unknown`, and live
execution is blocked pending real filesystem-quota evidence. A PVC request, its
capacity, free space, or a ResourceQuota does **not** prove that Docker Desktop
will refuse a write or inode creation at that value. Docker Desktop cost/billing
is recorded as `operator_attested_unknown`; that state blocks live execution. A
zero-dollar Tessera grant is an internal reservation, never a provider billing
cap, and cannot change either state.
NetworkPolicy enforcement and storage durability remain unproven by object
presence. Resources are retained. There is no teardown.

## Explicit in-cluster conformance probes

After setup, the separately opt-in `check_astra_kubernetes_conformance.py`
creates a **new, explicitly named** probe Namespace and PVC. It must never be
pointed at the retained Astra Namespace or `astra-outputs`: it does not write to,
modify, or delete either retained resource. The fixture builds and loads the
local digest-pinned image, creates only run-labelled probe resources, writes a
sentinel, deletes its writer Pod, and verifies the sentinel after a separately
created Pod remounts the new probe PVC. Its own deny-all and label-scoped allow
NetworkPolicies still require the allowed client to connect while the otherwise
identical denied client cannot.

The selected StorageClass must explicitly declare the exact quota profile with
these annotations before the fixture creates its Namespace:

```text
tessera.io/quota-enforcement: filesystem-quota-v1
tessera.io/byte-quota-bytes: "1048576"
tessera.io/inode-quota-files: "64"
```

Those declarations are only an admission prerequisite, not proof. The fixture
writes non-sparse bytes past the declared byte bound and creates files past the
declared inode bound inside the new PVC. Each probe records the actual write or
create failure, errno, attempted/actual amount, and declared limit. It accepts
only `ENOSPC` or `EDQUOT` at or below the declared bound. A PVC storage request,
PVC/PV capacity, or filesystem free-space value is never interpreted as a hard
quota. A StorageClass without the exact declaration, an unbound PVC, or a probe
that writes/creates past its declared bound is reported as `unsupported` with a
nonzero exit; it is not a pass.

Use a new evidence file in an existing private directory; all target arguments
are mandatory and there is no current-context or ambient live mode:

```sh
PRIVATE_DIR=/absolute/private/operator-directory
.venv/bin/python scripts/check_astra_kubernetes_conformance.py --run \
  --kubeconfig "$HOME/.kube/config" --context docker-desktop \
  --probe-namespace astra-quota-probe-20260914 \
  --probe-pvc astra-quota-pvc-20260914 --storage-class quota-fixture \
  --pvc-storage 8Mi --byte-quota-bytes 1048576 --inode-quota-files 64 \
  --evidence "$PRIVATE_DIR/astra-conformance.json"
```

The evidence records the new Namespace/PVC/PV/StorageClass identities, declared
bounds, StorageClass declaration, every created resource UID/timestamp, byte and
inode records, and the sentinel remount result. Cleanup rechecks ownership and
UID immediately before every narrow delete, verifies no run-labelled Pod,
NetworkPolicy, or PVC remains, then confirms the new Namespace is absent. A
replacement UID fails cleanup closed rather than deleting it. Kubernetes does
not expose a UID precondition on delete, so an adversarial same-namespace writer
between that check and delete remains a documented API limitation. The evidence
file is retained on pass, unsupported capability, or failure.

`attach` explicitly reports that no execution attachment occurs. `export-live-v2`
only writes a provider-v2 prerequisite manifest; it does not run the external
OAuth v2 runner or create any provider effect. Follow the separate
[remote v2 runner contract](remote-kubernetes-openai-chatgpt-oauth-e2e-v2.md)
only when its deployment, opaque runtime reference, preflight, and one-use human
confirmation prerequisites are available.
