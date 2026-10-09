# Fenced agent artifact and package capabilities

## Implemented scope and default

Schema **49** adds a tenant/project-scoped capability policy and operation ledger.
The API implements explicit declarations, human decisions, lease-bound admission,
fresh authority checks, revocation, and immutable attributed effect reports.
It performs **no network request, package installation, subprocess, secret
resolution, mount, or runtime policy change**. The default declaration has no
actions, destinations, registries, roots, credentials or byte/time budget.

All current responses carry `execution_enabled: false`. The existing CPU and
Kubernetes adapters do **not** implement the capability broker contract; their
network/installation guards remain unchanged. Even a fully approved policy is
not an instruction to relax a Job's network policy, mount the control PVC, or
run an install on the host. The [runtime broker](runtime-capability-broker.md) now
implements signed synthetic packages, streaming fixture checks, durable lifecycle
receipts and custody reconciliation. A future enforcing adapter is still required
before actual network downloads or installations are supported. There is no
agent-controlled enable flag.

This is separate from coding-agent development permissions and from dataset
provenance/license review, artifact classification/release, and scientific result
acceptance. None substitutes for another.

## Required declaration

An agent may **request** a declaration against an immutable task plan revision and
digest. Only a currently authenticated human may approve its exact declaration
digest, and only after that task plan is approved. A separate active execution
grant and worker lease are still required. Experiment execution continues to use
its existing task/variant admission; a capability policy cannot create a grant.

Each declaration names:

- Exact environment identity and SHA-256 image identity; admission also matches
  the image digest against the execution grant's pinned image.
- Exact public HTTPS destinations and ports, without wildcards, userinfo,
  query-string credentials, fragments, traversal, or encoded URL ambiguity.
- Explicit model and data registry prefixes; package manager (`pip`, `uv` or
  `conda`) and channel URLs. Each action must fall within its declared registry
  or manager/channel and exact destination/port.
- Explicit filesystem roots within `/workspace/artifacts` or
  `/workspace/packages`. These are **sandbox paths**, never permissions for host
  paths or control-plane storage with similar names.
- Aggregate byte/time ceilings and individual action ceilings. The sum of action
  ceilings cannot exceed the aggregate. Limits are at most 1 TiB total, 24 hours
  total, 1 hour/action, 128 actions, 32 destinations, and 16 entries in each other
  allowlist. Each admitted action permanently reserves its budget; retries and
  unknown outcomes do not refund it or create another operation.
- Each model, data, or package action's ID, name, exact version, source URL,
  expected SHA-256, byte/time limit and destination root.
- For packages, a pinned **lockfile SHA-256**, manager and channel. This slice
  chooses the lockfile branch of “install commands or lockfile”: arbitrary shell
  commands, package discovery, unpinned dependency resolution, source builds and
  executable installation hooks are not authorized. Every transitive package
  needs its own approved action. A broker must verify the complete lock closure
  and install each exact artifact offline, without dependency/index fallback.
- Opaque credential references, if any; absence means anonymous access, **not**
  permission to inspect the environment, home directory, kubeconfig, cloud
  metadata, service-account token, host keychain or control credentials.

For authenticated artifacts, a human must separately create the existing
credential-access lease, bound to the same worker, execution lease and epoch.
The reference must match the action, `audience` must equal the exact action URL,
`purpose` must be `artifact-read`, and scope must be exactly `action:read` plus
`sha256:<artifact hash>`. Scope order is irrelevant. The capability authority
cannot outlive that access lease. No raw secret is accepted or resolved by these
routes. Reference strings are identifiers, never environment-variable or file
lookup instructions.

## API sequence

Control routes use human/agent **control-audience** credentials; worker routes
require the separately enrolled **worker-audience** identity. All POSTs except
`authorize` require `Idempotency-Key`.

| Route | Purpose |
| --- | --- |
| `POST /tasks/{task_id}/capabilities` | Request `{expected_plan_revision, expected_plan_digest, declaration}`. |
| `GET /capabilities/{policy_id}` | Read the scoped policy and up to 128 operation/receipt records. |
| `POST /capabilities/{policy_id}/approve` | Human-only `{expected_revision, declaration_digest, expires_at}`; expiry must be future and within 24 hours. |
| `POST /capabilities/{policy_id}/revoke` | Human-only `{expected_revision}`; never claims termination. |
| `POST /worker/capabilities/{policy_id}/operations` | Reserve one exact action under `{execution_lease_id, expected_epoch, environment_id, image_digest, action, credential_access_lease_id}`. |
| `POST /worker/capability-operations/{operation_id}/authorize` | Fresh authority check; not a cached/replayable execution token. |
| `POST /worker/capability-operations/{operation_id}/receipt` | Record actual reported effects, including mismatches and late effects. |

Policy revision starts at 1, approval moves it to 2, revocation increments it.
Only one policy can **ever** be approved per task plan revision, including after
expiry or revocation. To change/widen it, revise and reapprove the plan; old
grants/leases then fail their existing revision fences. An approval replay
rechecks current policy authority and cannot resurrect a revoked policy.

An operation is unique per policy/action, including after worker restart or an
unknown outcome. An identical current admission replay returns the same intent;
a new key cannot reserve the action again. Changed bodies with the same key
conflict. Completed/expired/stale admission replays are rejected. The operation
deadline is the minimum of the action duration and policy, grant, execution lease
and credential-access expiries. Renewing a parent does not extend that deadline.

Every authority check rechecks the current human approver, task revision/digest,
grant, registered ready worker, workload identity, execution lease/epoch, input
release, pinned image, and optional credential-access lease. Cancellation,
retirement, revocation, expiry or plan changes fence use. An authority window is
at most **five seconds**, not proof of runtime isolation or successful execution.

## Trusted worker contract

`tessera_worker.capability_contract` contains pure, offline checks. Its
`FenceEvidence` must come from **trusted runtime inspection**, never the agent or
untrusted JSON. All evidence flags default false. `require_effect_authority`
rejects current disabled control responses, identity mismatches, missing fences
and invalid authority windows. The new broker's synthetic transport never calls
this real-effect gate with fabricated fence evidence; it has a separate signed
`synthetic` mode and cannot activate a live adapter.

An implementing broker must demonstrate all of the following before activation:

1. Non-root, read-only image; no privileged/host network/PID/IPC access; isolated
   mount namespace; no host/control-PVC/Docker socket mounts or ambient credentials.
2. Broker-only network egress; HTTPS GET only, TLS hostname verification, explicit
   port, no redirects, environment proxies, uploads or alternate registries. Check
   **all** DNS answers on every connection, deny private/metadata/special-use and
   multicast addresses, and pin the checked address to prevent DNS rebinding.
3. Streaming byte and wall-clock limits, including failed transfers; workspace
   quotas and no-follow directory-FD writes. Reject symlink/hardlink/archive
   traversal and archive/decompression quota escapes. Lexical root checks alone
   are not filesystem enforcement.
4. Artifact and staged lock hash verification before promotion/install; complete
   pinned transitive closure; offline no-dependency installation without shell,
   build scripts or post-install hooks. Unsupported manager/lock modes fail closed.
5. Durable operation-ID deduplication **before** any effect, with no new execution
   after ambiguous restart. Reconcile the existing operation instead.
6. Fresh authenticated control checks before each download/install and at least
   every five seconds while running. On revocation, deadline, disconnect or failed
   renewal, stop/kill the process and quarantine partial artifacts. Revocation
   cannot undo bytes already fetched or an install already performed. A cancellation
   request is not proof of termination.
7. Durable local effect accounting until the control receipt is acknowledged.
   Report failures and unexpected sources/paths/versions, not only successes.
   If workload authentication itself is revoked, the worker cannot upload a late
   report; preserve its journal for human reconciliation, never weaken auth.

## Audit and evidence

`capability.requested` and `capability.approved` events retain the full immutable
declaration, digest, requester and approver. Admission records exact requested
bindings and allow/deny events. Fresh checks and use denials are recorded.
State, events and idempotency writes share one SQLite transaction; a failed audit
write rolls back the state transition.

Receipts require `actual` action metadata, `bytes_downloaded`, `elapsed_ms`,
`outcome` (`downloaded`, `installed` or `failed`), `downloaded_sha256`,
`result_sha256`, and `installed_packages` (an empty list for non-package effects).
For packages the inventory is exactly the action's `{name, version,
artifact_sha256}` entry, and its result digest is Tessera's canonical JSON digest
of that list. Model/data result digests must equal the expected artifact digest.
Private destinations or forbidden roots in observations are retained as
violations, not admitted as new policy. Do not submit raw logs or credentials.

Each operation has one immutable final receipt. Exact retries return it without
new effects; changed final receipts conflict. Late/stale, failed, over-budget,
digest, inventory, version, destination or root mismatches are retained with
`assessment: quarantined` and revoke any remaining approved policy authority.
An intent without a receipt remains unresolved, not implicitly successful.

Even a matching receipt is only `assessment: reported`, with
`evidence: worker_reported_not_independently_verified`. It is not artifact
release, independent byte verification, runtime attestation, a completed
experiment, or scientific acceptance.

## Migration and verification

Back up stopped private state and explicitly run the normal `tessera migrate`
workflow. v48 → v49 creates empty policies/operations, invents no approvals, and
invalidates active-state pre-upgrade backup freshness. Fresh and migrated schemas
converge; backup/held restore preserves history but cannot enable mutations.

Offline acceptance tests:

```sh
.venv/bin/pytest -q tests/test_capabilities.py tests/test_capability_fencing.py tests/test_capability_contract.py
```

These tests simulate effects and authority transitions in temporary SQLite state.
They do not download, install, resolve secrets, launch containers, or prove a live
fence. No live downloads or installs were run for this implementation.
