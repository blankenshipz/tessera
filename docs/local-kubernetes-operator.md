# Local Kubernetes operator admission (partial)

`tessera-k8s-operator` is a **separate human-operated stdio MCP**. It must not
be added to a research agent's MCP configuration. `local_control_inspect`
reads fixed Docker Desktop context metadata via the
private kubeconfig (`$KUBECONFIG`, default `~/.kube/config`), the dedicated prospective namespace
`tessera-local-persistent`, the two occupied historical namespaces, and private
state *metadata* under `$TESSERA_STATE_DIR` (default `<repository>/.tessera`). The kubeconfig is
parsed locally but credential values are never returned or logged; owner state
files are checked by metadata only. An absent namespace is only an observation,
not a reservation or permission to adopt it later. `local_image_plan` hashes
reviewed build inputs and explicit target identities. `local_image_build` requires
an exact eligible-human-approved plan digest; merely echoing a tool result is not
human approval. It builds with the dedicated Dockerfile-specific source allowlist,
loads the result into the named Docker Desktop node, and returns its pinned node
manifest reference. Build/download effects are possible, but no cluster resources
or control writers are created. A failed build/import retains artifacts for
manual reconciliation.

`local_resource_plan(image)` requires the pinned immutable image to exist in the
named node and shows the exact inert documents plus an independent digest bound
to the kubeconfig, cluster namespace UID, legacy namespace UIDs, Docker endpoint
and fixed target. `local_resource_provision(approved_plan_sha256, image)` requires
separate eligible-human approval of that **resource** digest, never reuses image
approval. It creates only the absent namespace and three named inert resources;
an exclusive private intent receipt is written before any cluster effect, and
the namespace UID/nonce is checked before each subsequent create. On partial
success it deliberately stops without adopting, deleting or overwriting anything;
inspect receipt/resource UIDs manually before a separately reviewed recovery.
This is not an idempotent automatic retry and cannot activate a writer.

`local_backup_plan` and `local_backup_source(expected_plan_sha256)` create a
one-time, private, consistent online backup of the existing local source. The
digest is a stale-input integrity check, **not** another human-approval gate:
backup/migration has already been authorized. `local_backup_verify` rereads the
manifest, SQLite integrity and every declared object hash. The validated bundle
includes referenced project objects and does **not** include
`owner.header`, stop the local process, transfer active credentials or activate
Kubernetes. The fixed backup destination must be absent; failed attempts retain
their private output for inspection.
`local_cutover_preflight` is read-only and demands the verified snapshot, receipt
namespace/claim/Deployment/Service UIDs, a Bound PVC, zero running target replicas,
and the exact local source listener/process identity. It does **not** fence or
signal the source process, copy private owner authority, or activate a service.
`local_cutover_fence` exists for a later gated cutover: it refuses until the
inert, exact-UID resource receipt and verified backup exist. It stops only the
identified local listener, checks exit and source file handles, moves only
`tessera.sqlite3`, `owner.header`, `project-artifacts` and any SQLite sidecars
into a private same-filesystem quarantine, and leaves all unrelated `.tessera`
entries in place. A private marker makes newly built Tessera refuse that old
state; old binaries fail because the database is absent. Its fresh post-stop
snapshot uses the non-source-writing backup primitive. On a partial failure the
quarantine/marker remain for inspection: no automatic cleanup or second signal.
`local_cutover_fence_absent_writer` is a **different** destructive tool for a
source whose original PID has disappeared. It requires the same verified backup,
owned Bound PVC and inert target, then checks the port, process inventory and
exact file handles before and after writing a durable authority marker and before
and after quarantine. It compares source inode/size/mtime across checks, moves
only the named private nodes, fsyncs the source/quarantine directories and makes
a fresh non-source-writing snapshot with readback. It sends **no signal**. A
reoccupied port, matching process candidate, changed file or partial rename
retains the marker/quarantine for manual reconciliation; it does not retry or
unfence. These observations cannot exclude an arbitrary same-UID writer or a
supervisor restart between checks. Confirm restart suppression independently,
and obtain separate human approval before invoking either fence tool. The
absent-writer mode has **synthetic tests only**, no live fence has been performed.
`local_cutover_rollback` restores only while the exact namespace still has no
Pods, the Deployment has never been updated and has zero replicas, and no
activation marker exists. Neither action has been exercised on the live writer.
These checks cover the operator-owned path, not an unrelated administrator
manually mounting the PVC and later deleting that Pod; rollback requires
independent admission that no such out-of-band writer has run. The PID check
also cannot provide a kernel-atomic process handle on macOS: a changed PID
identity is rejected before signalling, and the port/file guards are rechecked
afterward, but a PID-reuse race must be treated as an uncertain partial result.

**No live control deployment capability exists yet.** After inert provisioning,
backup and fence, the operator still must privately transfer active
state including owner authority and referenced objects without exposing values
to MCP, run an explicit schema migration if needed, and deploy one Recreate
writer. The staged `../deploy/kubernetes/local-persistent-control.yaml` has a
placeholder image digest, `replicas: 0`, RWO PVC, Recreate strategy, and ClusterIP
Service. It is **not safe to apply unchanged**. Add a bounded authenticated
Kubernetes port-forward bound only to Mac loopback, then verify readiness,
backup custody and Pod-replacement recovery. HTTPS is required if a later route
is non-loopback. `tessera restore` is
permanently held recovery history; it cannot be used as active state transfer.
The staged control container now binds **Pod loopback** `127.0.0.1:8765` so
local-only Tessera serving rules can apply through an explicit Mac-loopback
Kubernetes port-forward to the Deployment, not an exposed Service. The inert
ClusterIP Service cannot reach that listener and must not be advertised as a
working client route. A default-deny NetworkPolicy declaration is included,
but policy readback is not enforcement: no credential-bearing importer Pod may
run until an actual deny witness or a separate private node-side mount route
has been admitted. No credential-bearing importer Pod exists.
`local_pvc_bind` can create exactly one **credential-free** binder Pod with the
reviewed immutable image and named PVC to satisfy `WaitForFirstConsumer`. It
requires the private operator receipt and checks namespace/PVC/Deployment/policy
UIDs; a durable binder intent is written before create. `local_pv_inspect` then
checks binder Pod UID, Bound claim UID, claimRef, `standard` storage, exact
local-path host path and `desktop-control-plane` node affinity. It reads no PVC
contents. `local_pvc_release` uses a kubeconfig-verified private HTTPS API DELETE
with server-side UID and resourceVersion preconditions, waits for no Pods, then
rechecks PV identity. `local_private_import` requires a quarantined source and
verified post-fence snapshot, a prior release receipt, no target Pod, exact owned
PV host path and an empty state child. It streams fixed regular-file entries over
Docker Desktop node stdin; credentials never enter argv, MCP output, or logs.
Readback compares snapshot/object/owner hashes and UID 65532 private modes. These
actions have **synthetic tests only**: no real binding, deletion, or import has
been executed. Every attempt writes a private intent first; failures retain
partial state for reconciliation and cannot be retried/adopted automatically.
`local_activate` checks the completed import receipt against the fenced snapshot,
private owner hash, exact namespace/PVC/PV/Deployment and source quarantine. It
writes a durable marker before an atomic JSON Patch that tests Deployment UID,
resourceVersion, image, and replicas=0 then requests replicas=1. That marker
blocks automatic old-source rollback even if the patch fails ambiguously.
`local_ready` checks the sole Ready Pod's ReplicaSet ownership and fixed image;
`local_loopback_route` starts one kubeconfig-authenticated port-forward to that
Pod bound only to Mac `127.0.0.1:18765` and checks `/health/ready`. Port-forward
is **not** HTTPS and is intended only for local Mac clients; application owner
authentication remains a separate client check. Route reconnect inspects the
recorded PID's start time and exact fixed command and confirms the local listener
belongs only to that PID; a replaced Pod's verified old route is stopped before
another route is bound. Foreign processes and uncertain launches are never
adopted or killed. `local_target_backup_before` invokes Tessera's SQLite online
backup **inside the owned running Pod** and streams only its declared snapshot
and object bundle to a create-only private directory on the Mac; no `owner.header`
is archived. `local_pod_recreate` requires that verified independent backup,
then sends one UID/resourceVersion-preconditioned DELETE of the fixed owned Pod;
`local_target_backup_after` verifies the replacement Pod on the same PVC and
compares control incarnation, principal/project/credential records and the prior
event prefix across the two private snapshots. A failed attempt retains its
intent and partial bundle for reconciliation. These methods have **synthetic
tests only**; no activation, route, backup, or restart has run live.
Do not invoke the write actions before exact eligible-human image/resource
approvals, reviewed source fence, and a controlled cutover sequence.
Never apply the lab bootstrap to an occupied namespace or mutate an existing
control PVC as a substitute.

## Mac client route supervision

`deploy/kubernetes/dev.tessera.local-route.plist` is a template user
LaunchAgent for this checkout, not a research-agent MCP configuration. It runs
`python -m tessera_operator.route_supervisor` at login and every 60 seconds.
Each serialized, time-bounded tick opens a separate operator stdio MCP and calls
only `local_loopback_route`. That tool revalidates the activated namespace,
Deployment, image, PVC/PV, sole Ready Pod, fenced source and recorded route
process before retaining or replacing a route. The wrapper never calls activate,
reads application tokens, deletes Pods, or directly invokes kubectl. A guarded
refusal is reported as blocked, not bypassed on the next tick. Uncertain durable
route intents still require manual reconciliation. Launchd does not kill the
detached verified port-forward when a tick exits.

Install this profile only after checking ownership, absence of a conflicting
LaunchAgent and the existing route receipt/listener. It needs the logged-in
user's Docker Desktop and Kubernetes context; it is not Mac reboot/cluster
availability proof. Its private, metadata-only status lives at
`.tessera/private/local-route-supervisor-status.json`. No stdout/stderr bodies
are retained. Disabling the LaunchAgent stops future reconciliation, not the
existing verified port-forward; do not kill an arbitrary port owner.

The research MCP uses `tessera_mcp.server` (or `tessera-mcp`) with
`TESSERA_CONTROL_URL=http://127.0.0.1:18765` and an existing private
`TESSERA_AGENT_TOKEN_FILE` reference. Do not add the operator server to that
configuration. This neither extends credential expiry nor enables native desktop
credential persistence. The native desktop still requires Connect, the loopback
endpoint and selection of its human credential header through the native picker.
