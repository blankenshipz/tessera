# 0030: Direct upstream Agent Sandbox, one-shot network-none profile

Status: implemented isolated adapter; live activation remains operator-gated.

Reuse Kubernetes SIGs [Agent Sandbox v1.0.6](https://github.com/kubernetes-sigs/agent-sandbox/tree/cd0761d5001baaf293460cd382ee318744d6b249),
commit `cd0761d5001baaf293460cd382ee318744d6b249`, API
`agents.x-k8s.io/v1beta1`. Upstream is Apache-2.0, copyright the Kubernetes
Authors. Tessera uses its public API, not vendored controller source. Pin the
actual CRD spec/UID and controller Deployment UID/digest image independently;
the version string alone is not measured provenance.

Do not use Claim: this version requires a warm pool and Claim expiry can delete
the Sandbox and its owned PVC. Direct Sandbox expiry explicitly uses `Retain`;
stop uses `Suspended`, never object deletion. There is no new operator, CRD,
warm pool, router, general network profile, Secret delivery or arbitrary Pod exec.
Existing batch Jobs and `kubernetes_dispatches` remain unchanged.

The human-reviewed `agent-sandbox` plan freezes the tenant/project namespace,
target identity, strong RuntimeClass/handler, independent witness identity and
digest, image (existing grant pin), CPU/memory/storage and network-none profile.
No later mutable template lookup is permitted. The adapter emits one digest-pinned
non-root container with read-only root, dropped capabilities, no privilege
escalation, no token, no host namespaces or hostPath, and a PVC output mount.

Use the existing authenticated worker, grant, lease/epoch, immutable runtime
package, external-operation intent and tenant/event/idempotency paths. Schema 56
adds one binding keyed by external operation, not an attempt-required Job row.
Persist create intent and its claim before effects; lost responses permit exact
name/UID lookup only. Create suspended. Running may create a gated Pod; a separate
release rechecks current authority, full owned-Pod discovery, exact profile,
identity, runtime/witness and **all** additive selecting NetworkPolicies. Its
one-UID fence is committed before the gate-removal effect. Never release a
replacement or retry an uncertain gate removal. SQLite serializes local
revocation against the final current-authority check/effect; operator admission
and exclusive namespace writers are still mandatory across Kubernetes writers.
Read-only discovery is outside the control writer with a total monotonic bound;
final authority/identity/CAS effects have a short total transport budget. The
normal digest-approved `serve` configuration composes the real pinned-CA,
no-proxy HTTPS client and authenticated independent witness-reference verifier.
Absent/invalid configuration stays disabled, not a fake provider path. Explicit
priority zero and the standard preemption policy are fixed profile fields.

This initial one-shot profile freezes the initial lease expiry as upstream
`shutdownTime` (bounded also by hard expiry). Lease renewal cannot extend that
frozen package. A fresh observation of invalid authority requests suspension.
This is not a long-lived session supervisor; broader sessions require integrating
the verified independent supervision contract rather than relying on upstream
Pod replacement or an available flag.

Suspension status or Pod absence alone is not termination proof. Fresh matching
generation, complete owned-Pod pagination, no ownership/status contradictions,
and retained UID history are required. A released Pod must have observed terminal
container evidence on the same healthy Node UID. Lost/force-deleted/unknown-node
execution retains custody and needs independent operator termination recovery;
this adapter does not manufacture that evidence. A typed neutral collection
handoff is not collected bytes and never disguises a Sandbox as a Job.
Explicit governed delivery may consume that handoff with a distinct read-only
strong-runtime collector Pod, reusing existing collector/delivery journals,
installed-image byte collection and private artifact placement/manifest APIs.
Fresh producer/PVC proof, immutable collector UID/profile and healthy same-Node
terminal evidence are required. Retain reader Pods/journals on uncertainty;
there is no automatic collector deletion, arbitrary exec or public release.

Cleanup is a separate human command. Require an exact checksummed durable upload
manifest matching the frozen operation/outputs, completed artifacts, active
project placement/retention, verified stored bytes and fresh quiescence. Private
quarantined placement is sufficient; scientific acceptance/public release is
not required. Commit cleanup intent before UID/resourceVersion-preconditioned
deletions. Ambiguity retains the journal and does not claim object absence.
Session close/logout does not delete anything.

See the [operator and authenticated CLI runbook](../agent-sandbox.md) for the
remaining live prerequisites and deliberately unsupported paths.
