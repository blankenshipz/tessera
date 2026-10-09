# Opt-in coordinator and dispatch worker (operator-provisioned composition)

## Disposable Docker Desktop foundations (opt-in effects)

`scripts/bootstrap_local_kubernetes_composition.py` progresses beyond the
read-only preflight: with `--run` it atomically **creates**, never adopts or
replaces, the absent `tessera-lab` and `tessera-workloads` namespaces, checks
their Kubernetes UIDs and disposable nonce label, then creates the control and
workload foundation objects. It requires an explicit absolute non-symlink
Docker Desktop kubeconfig bound to a local CA-verified API, and the exact
node-resident `docker.io/library/tessera-local-lab@sha256:<64 hex>` image.
Build the repository `Dockerfile`'s `lab` target locally, import to the Docker
Desktop Kubernetes node, and independently verify that immutable reference in
node containerd before invoking. It sets `imagePullPolicy: Never`; there is no
registry fallback. The command is:

```sh
.venv/bin/python scripts/bootstrap_local_kubernetes_composition.py \
  --run --kubeconfig /absolute/path/to/docker-desktop-kubeconfig \
  --image docker.io/library/tessera-local-lab@sha256:ACTUAL_NODE_IMAGE_DIGEST
```

The command intentionally stops at `foundations_applied_not_ready`. On any
failure it does **not** clean up: record the printed namespace UID/nonce and
inspect the exact objects. Never delete an occupied namespace merely because
its name matches; cleanup needs a separate UID/nonce readback and an explicit
owned-resource decision. A preflight absence observation is not a reservation;
concurrent namespace creation causes the create to fail closed. This is not a
complete deployment. Provide a real TLS gateway and CA Secret, freshly issued
project-scoped agent and worker credentials, cluster API binding, actual
allowed/denied network probes and dispatch target evidence, plus admission and
storage checks before enabling coordinator/worker. Do not infer NetworkPolicy
enforcement from foundation objects. The authenticated deterministic Job and
receipt must be observed separately; this command neither starts a worker nor
claims one. No paid provider or third-party secret is involved.

`lab-control-plane.yaml` remains a single-writer provider-disabled control
profile. `provider-disabled-coordinator.example.yaml` adds an independently
restarting consumer process, not a model provider or Kubernetes dispatch worker.
The latter example is intentionally **not self-contained**: before applying it,
an operator must supply a built/pinned `lab` image, an existing project-scoped
agent token and project ID, an existing Kubernetes Secret named
`tessera-existing-coordinator-credential` with `agent-token` and `ca.crt` keys,
and a same-namespace TLS gateway Service `tessera-control-tls` backed by Pods
labelled `app.kubernetes.io/name=tessera-control-tls`. The gateway needs an
operator-managed certificate with that Service DNS name and a protected route
to the control Service on port 8765. Its ingress and control-hop NetworkPolicies
must be separately scoped and verified; the sample egress rule alone does not
make it reachable. Never put bearer credentials in a ConfigMap or image.
Only the gateway Pod gets `tessera.io/control-hop: "true"`; never apply this
label to the credential-bearing coordinator or dispatch worker. They use
`tessera.io/tls-control-client: "true"` for DNS and their own TLS gateway egress.

The sample process calls the existing authenticated `/me` endpoint when it
needs to read context, and sends a heartbeat and one claimed turn per cycle. It
has no provider; the response is explicitly provider-disabled. A valid agent
credential alone does not grant approvals, execution or paid model calls.
Kubernetes dispatch is now an independently restarting process in
`remote-dispatch-worker.example.yaml`, using the normal authenticated worker
claim/renew/report routes and the durable Job and claim journals on its own PVC.
The coordinator remains provider-disabled. The worker does not turn that into
provider execution, scientific completion or multi-region availability.

Apply only after provisioning the above prerequisites, editing the image and
project ID, validating policies and rendering the manifest for the intended
context. Use explicit `--kubeconfig` and `--context` flags; do not use ambient
current-context fallback. Never apply the placeholder example unchanged.
Docker Desktop Kubernetes on a laptop is not laptop-off availability.

## Workload target prerequisite gate

`workload-target.example.yaml` defines a **separate** namespace, deny-all
NetworkPolicy declaration, output PVC and small object/storage quota. It is not
a dispatcher, a Job-creation RBAC grant, a provider integration, or proof of
NetworkPolicy enforcement. Explicitly verify cluster admission restrictions,
workload identity, actual network-denial probes, target binding and storage
durability before enabling dispatch. In particular, do not grant the control
namespace Job creation or access to another writer's state.

## Owned TLS client network probe preparation (not dispatch)

After the existing TLS gateway Pod/Service and a private CA-only Kubernetes
Secret containing `ca.crt` are provisioned, the opt-in script below creates
**only** a fresh pair of control-namespace Python client Pods and two narrow
probe policies. It does not read the CA Secret, use credentials, patch existing
objects, clean up, create a worker, or authorize a Job. Confirm the pinned
`tessera-local-lab` image supports Python in the node; `imagePullPolicy: Never`
does not fetch it. Replace only the named, observed target values:

```sh
.venv/bin/python scripts/prepare_local_kubernetes_probes.py --run \
  --kubeconfig /absolute/path/to/kubeconfig \
  --control-uid OBSERVED_CONTROL_UID --workload-uid OBSERVED_WORKLOAD_UID \
  --nonce OBSERVED_BOOTSTRAP_NONCE \
  --image 'docker.io/library/tessera-local-lab@sha256:OBSERVED_DIGEST' \
  --ca-secret OBSERVED_CA_SECRET_NAME --gateway-pod OBSERVED_GATEWAY_POD_NAME
```

The script refuses UID/ownership changes and returns the random names and UIDs
of created resources. A partial create is retained for UID-scoped inspection,
not automatically retried/deleted. **Immediately**, while both Pods are running
and younger than four minutes, use the returned names in:

```sh
.venv/bin/python scripts/gate_local_kubernetes_network.py --run \
  --kubeconfig /absolute/path/to/kubeconfig \
  --control-uid OBSERVED_CONTROL_UID --workload-uid OBSERVED_WORKLOAD_UID \
  --nonce OBSERVED_BOOTSTRAP_NONCE \
  --allowed-pod RETURNED_ALLOWED_NAME --denied-pod RETURNED_DENIED_NAME \
  --gateway-pod OBSERVED_GATEWAY_POD_NAME \
  --ca-path /run/tessera-ca/ca.crt
```

The gate makes a CA-validated HTTPS readiness request from the allowed Pod,
then a TCP request to the **same live gateway Pod IP** from the denied Pod,
then repeats the positive. It rechecks namespace UIDs/nonce, probe/gateway Pod
UIDs and IP, Service UID/selector, policy UIDs/specs and client spec/CA mounts.
Any connection from the denied Pod (including HTTP 200), failed positive,
missing identity, stale Pod or changed policy blocks. The report is a witnessed
**control-gateway TLS path only**; it is neither workload-namespace network
enforcement evidence nor a `KubernetesClusterConfig` target binding. The
workload policy, admission/storage restrictions, live worker target, scoped
credential/Secret, Deployment, deterministic Job and receipt remain separate
prerequisites. In particular, do **not** dispatch based on this report when
the CNI permits the denied client.

For a **separate workload-namespace** witness, after checking the source image
is pinned and the owned `outputs` PVC is Bound, the opt-in command below creates
only three short-lived probe Pods and two narrow policies. It rechecks the
namespace/kube-system UIDs, owner label, default-deny and PVC before effects,
and rechecks exact created Pod/policy UIDs and specifications and the PVC before
returning a typed `network_evidence` JSON object. It brackets the denied TCP
connection to the same live server Pod IP with two successful allowed connections.
No credential, worker, Job, target file or simulated observation is created:

```sh
.venv/bin/python scripts/witness_local_workload_network.py --run \
  --kubeconfig /absolute/path/to/kubeconfig \
  --workload-uid OBSERVED_WORKLOAD_UID --nonce OBSERVED_BOOTSTRAP_NONCE \
  --image 'docker.io/library/tessera-local-lab@sha256:OBSERVED_DIGEST'
```

The resource names/UIDs are returned for exact-owned reconciliation; this script
never deletes or retries a partial create. The 15-minute typed evidence lifetime
does not waive workload admission, storage-isolation, internal API TLS target
binding, fresh scoped worker credentials, Kubernetes Secret, Deployment or
authenticated deterministic Job/receipt acceptance. A denied connection that
actually succeeds fails closed, even if policies read back unchanged.

After an operator has provisioned a target, the read-only gate checks exact
named namespace/PVC/policy prerequisites without reading Secrets or logs:

```sh
.venv/bin/python dev/check_dispatch_target.py \
  --kubeconfig /absolute/path/to/kubeconfig --context EXPLICIT_CONTEXT \
  --control-namespace tessera-lab --workload-namespace tessera-workloads
```

The gate returns observed resource UIDs only and calls its verdict
`prerequisites_observed_not_enforcement_proven`. Its result cannot replace the
typed cluster identity and network enforcement evidence required by
`KubernetesClusterConfig`, or create a worker-scoped credential.

## Remote dispatch installation prerequisites

After a **fresh real allow/deny/allow network witness** has passed (a timeout
or successful denied connection is not a pass), an operator may enroll a
project-scoped worker without touching Kubernetes. Prepare the explicit
`target.json` described below from observed identities, not example values,
and invoke the opt-in owner API enrollment once. Typed target JSON alone is
not proof the network was enforced; the operator must validate the live gate
results and resource identities independently before supplying it:

```sh
.venv/bin/python scripts/enroll_remote_dispatch_worker.py \
  --control-url https://CONTROL-GATEWAY --control-ca-file /private/control-ca.crt \
  --owner-token-file /private/owner-token --target-file /private/target.json \
  --private-dir /private/dispatch-enrollment --project-id PROJECT_ID \
  --build-digest EXACT_SHA256_OF_DEPLOYED_IMAGE_BUILD
```

The owner-token file must be private `0600`. This requires the existing local
owner's authorization and TLS gateway; it sends two bounded authenticated
control commands (`/credentials`, project worker registration), never a Job.
It retains the exact token and idempotency keys in private `0700` custody so a
lost response can be replayed with the same identity. It prints only worker
identifiers; `worker-token` must be transferred securely to the operator-created
Secret referenced by the sample Deployment, without printing it in logs or
putting it in a command argument. The example Deployment/RoleBinding are
operator-managed opt-in resources, not applied by this command. Enrollment
alone issues no grant, lease, admission, approved route or dispatch; those
continue through the authenticated human and worker APIs. Before starting
the Deployment, verify its image/build, scoped service account, exact target,
TLS gateway, separate journal PVC and fresh enforcement gate; do not deploy
or run a Job on a timeout. Credential expiry defaults to one hour. Before expiry,
the local owner may renew the same active credential via
`POST /credentials/{id}/renew` with a reviewed `expires_at` and idempotency key;
token/principal/journal identity is preserved. Renewal cannot revive an expired
or revoked credential, outlive its parent, extend execution authority, or refresh
network evidence. There is no unattended owner-token renewal in the worker.
After expiry, use fresh owner-approved enrollment and reconcile old work rather
than rewriting uncertain intent; see [installation](../../docs/installation.md).
The deployed worker's queue mode claims only its own
approved dispatch, reports actual adapter observations and reconciles its
durable original Job UID; no synthetic completion is emitted.

The example is **not deployable unchanged**. Supply and validate, for the
identified cluster, an image with this Python package, a TLS gateway to the
control API with trusted operator CA, worker-specific control principal/grant
and credential, a separate workload namespace/PVC and admission policy, a
dedicated durable worker-journal PVC, an exact pinned workload image, and real
fresh allowed+denied connectivity probes. The cluster's API server IP/CIDR and
port must replace the sample documentation address/port. The sample RBAC has
only named namespace/PVC/policy reads and namespace-scoped Job and Pod verbs;
it cannot create Secrets, approve dispatch, or write control state. Verify
admission restrictions and NetworkPolicy enforcement separately. In particular,
the example policy's API server `192.0.2.1/32` is a nonroutable placeholder.

Create `tessera-existing-dispatch-binding` in `tessera-lab` with `worker-token`,
`target.json`, `kubeconfig`, `ca.crt` (the cluster CA, if needed for independent
inspection), and `control-ca.crt` (the TLS gateway CA). The target's `cluster`
is the exact `KubernetesClusterConfig` fields excluding `network_none_evidence`;
`cluster.kubeconfig` must be `/run/secrets/tessera/kubeconfig` and its SHA256
must match those file bytes. The named kubeconfig context must resolve to the
same HTTPS server and CA. `network_evidence` has the
`KubernetesNetworkPolicyEvidence` fields (`capability`, `policy_name`,
`policy_uid`, `policy_spec_sha256`, `cluster_uid`, `namespace_uid`,
`observed_at`, `allowed_connectivity`, `denied_connectivity`); each probe has
`source_pod_uid`, `destination_pod_uid`, `outcome`, `timeout_seconds`,
`observed_at`. Supply actual witnessed probes, never generated stand-ins.
`output` has `pvc_name`, `mount_path`, `pvc_uid`, `pvc_spec_sha256` (canonical
JSON SHA256 of the live PVC spec); use the dedicated workload `outputs` PVC.
Network evidence expires for new admissions after 15 minutes; already-owned
jobs remain observable and cancellable if their bound target is unchanged.

The Secret's projected worker token is copied at startup to the persistent
worker-private directory as an owner-only 0600 file; the durable journal
persists across Pod restarts. The Kubernetes API token is separately projected
for the narrow ServiceAccount and is never part of Job manifests or worker
reports. The worker invokes `python -m tessera_worker.kubernetes_remote_service`
with explicitly mounted target/CA/token/credential paths. The Python HTTPS
transport has bounded responses and no ambient proxy, redirect, context,
credential resolution or automatic retries. It checks named cluster/namespace,
PVC and policy identity before effects, submits only the frozen operation Job,
and deletes with a Job UID precondition. It does not collect output bytes: the
existing artifact handoff is metadata only, and research acceptance remains
separate. Bounded smoke: supply exact explicit paths and `--max-cycles 1`;
without that flag the process continues processing the queue.

The cluster RoleBinding and Deployment are operator-managed administrative
actions, **not** authorized by an approved research dispatch. The sample
cannot prove off-laptop availability without an independently running remote
cluster/control gateway and real observed jobs. Docker Desktop is a local
development target only. No Secret, gateway, target evidence, or image is
generated by this example.
