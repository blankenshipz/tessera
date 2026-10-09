# Runtime capability broker: offline implementation and activation boundary

## What runs now

`tessera_worker.capability_broker.CapabilityBroker` executes **synthetic**
download/install steps from caller-supplied fixture bytes. It does not use a shell,
package manager, socket, container engine, provider, credential resolver, environment
proxy or home-directory configuration. Default transport is `FakeCapabilityEffects`.
`GatedRealCapabilityEffects.preflight` always returns blocked by raising
`BackendBlocked`, even with `enabled: true` and injected client/runner objects.
No current code claims to establish a trusted sandbox.

The [sandbox activation work](capability-sandbox-activation.md) adds independently
testable candidate-manifest, pinned-HTTPS and watchdog-protocol primitives. It does
not yet provide an independent live inspector or enforcing runner and does not
change this blocked activation boundary. Signed probe claims alone cannot activate.

The broker is a library boundary for a trusted supervisor, not a new HTTP/CLI agent
endpoint or automatic integration into CPU, Kubernetes or outcome0 execution.
Existing provider-disabled responses and network guards are unchanged. Salad is
not an activation target. Synthetic completion means the offline state machine
verified fixtures/inventory, **not** that a package was installed or a model fetched.

## Authority and immutable packages

1. Request/approve a capability policy through the existing human-gated ledger.
   For broker use, `declaration.broker_basis` must contain SHA-256
   `protocol_digest`, `data_digest`, `code_digest`, `environment_digest`. These are
   explicit approved identities, not inferred provenance or claims of independent
   inspection. The data digest must equal the grant's released snapshot digest;
   environment identity/image is also checked against the grant by existing policy.
2. Each action additionally needs `expected_size` and canonical `content_type`,
   e.g. `application/octet-stream`. Broker v1 caps an action at **16 MiB**, one hour,
   and one concurrent operation per worker/environment within a tenant/project.
   Policy aggregate limits, exact URL/port/registry/channel, pinned artifact/lock
   hashes and `/workspace/artifacts` or `/workspace/packages` roots still apply.
   No credential references are supported in broker v1, even if otherwise approved.
3. Admit the action with its current grant/worker/lease/epoch. Explicitly compose
   `CapabilityBrokerStore(state_dir, signing_key=...)` on the **control side** and
   call `issue(worker, operation_id)`. A key must be at least 32 bytes and remain
   in control-only custody. There is no default/generated key, environment lookup,
   key provisioning command or HTTP issuance route in this slice.
4. The returned frozen `SignedCapabilityPackage` contains canonical immutable JSON
   bytes, their SHA-256 and a domain-separated HMAC-SHA256. The payload binds mode,
   org/project/task/worker, operation/policy/grant, plan/declaration digest, full
   declaration, lease/epoch/environment binding, absolute expiry and concurrency.
   Expiry is the existing operation deadline bounded by all parent authority.
5. `InProcessBrokerAuthority` composes offline tests with actual authenticated
   control-store checks. It is **not** a remote transport and must never be given
   to a workload together with control files or signing material. Production will
   need a separately authenticated, bounded-latency control channel.

The broker's configured `(org_id, project_id, worker_id)` is independent of the
submitted package. It validates canonical bytes/digest, calls control to validate
the signature and live bindings before claim, and rechecks authority/deadlines
before and after each bounded step, including reads, install and publication.
There is no authority cache. Revocation fences the *next* effect; it cannot undo
an already in-flight call. A delayed reply cannot extend signed expiry.

The immutable operation ID is claimed in the existing control transaction and
idempotency ledger for exactly one persistent journal identity. A second journal
cannot replay that effect. Outstanding claims also serialize different operations
on that worker/environment; only acknowledged, matching completed evidence with
confirmed termination releases the slot. Failed, stale, conflicting and unknown
evidence stays fenced. One lifetime OS file lock and
one broker attachment serialize the private worker journal locally.

## Downloads and installs

The transport resolves before opening; all addresses must be public and the exact
approved HTTPS URL/port must match. The checked address tuple is passed to `open`;
response URL/address tuple must still match. Redirects, private/metadata addresses,
alternative registries, content-type changes and size changes fail. A future real
client must actually pin those addresses and verify TLS hostnames, disable proxies,
redirects and decompression, and enforce the supplied per-call timeout. The fake
client does not establish those live properties.

Reads are at most 64 KiB and bounded by the expected size, with a one-byte end probe
to reject a lying length. Each accepted chunk and its progress receipt commit in
one SQLite transaction. Out-of-bound chunks are rejected, not retained beyond the
budget. EOF size and streaming SHA-256 must match before publication. Retained
partial/mismatched bytes remain quarantined and cannot become completed artifacts.

Install v1 accepts only pip/uv `.whl` declarations with a **single-artifact complete
closure** represented by a hash-pinned JSON lock:

```json
[{"name":"fixture","version":"1.0","artifact_sha256":"<64 lowercase hex characters>"}]
```

Hash the exact lock bytes (maximum 64 KiB). The parsed list must equal the one
approved package; additional dependencies, unpinned entries, source builds, Conda,
hooks, arbitrary commands and dependency/index fallback are unsupported and blocked.
Install uses a package-derived isolated target identity under the approved packages
root. Completion requires exact returned name/version/hash inventory, matching
target identity, verified result, exit code zero and confirmed termination. This is
a **fake inventory verification seam**, not validation of a live installed tree or
wheel safety. Interrupted/lost installs are failed or unknown, never successful.

## Worker journal, custody and reconciliation

Create a private 0700, supervisor-owned journal root on persistent local storage;
no workload may mount/access it. Parent directories are deployment-controlled.
Symlink roots/ancestors and non-private or linked database files are rejected.
The journal creates a 0600 SQLite database with synchronous FULL and schema version
1; unsupported existing versions fail closed. The deployment must supply a correct
local filesystem/fsync/locking implementation (not shared NFS semantics).

The journal retains the signed package, operation intent, bounded byte chunks and
a hash-chained append-only receipt sequence. Receipt states are requested, approved,
started, progress, completed, failed, cancel_requested, cancelled, unknown and
blocked. Records include approved artifact/package identities, lock hash, limits,
timestamps, elapsed time, byte counts, request/response/effect digests, exit code,
verified inventory and termination status. Cost and usage remain `null` (unknown).
No raw exception text, logs, headers, credentials, tokens or response URLs enter
receipts; untrusted response metadata is represented by a digest.

Successful verified fixture bytes are atomically published **without overwrite**
into a tenant/project-scoped journal custody manifest with their completion receipt.
This is private worker byte custody, not an agent-writable sandbox path or a release.
The journal never deletes any chunks, packages or manifests. No broad cleanup runs.

Call `journal.reconcile(package, sink)` where the offline control sink calls
`CapabilityBrokerStore.reconcile(worker, package, bundle, content)`. Control:

- Revalidates signature, worker/tenant/project scope, receipt chain and retained
  bytes. Historical evidence can transfer after lease expiry/revocation, but not
  after workload authentication/project access is revoked.
- Publishes the exact retained bytes to `FilesystemProjectArtifactBackend`, using
  deterministic hashed tenant/project/artifact identity and its atomic no-overwrite
  publication. Even failed/partial evidence remains private and quarantined.
- Commits the complete lifecycle bundle, object identity and idempotency record to
  the existing audit sink in one control transaction. A repeated identical bundle
  returns the same acknowledgement; a changed bundle conflicts. Control independently
  compares retained hashes/sizes and reported identities, limits and inventory to
  the signed action. Incomplete, stale or mismatched effects revoke remaining policy
  authority without claiming termination or releasing the global slot.

Project object publication and control SQLite commit are **not** a distributed
transaction. If audit fails after publication, retain the worker journal and retry
only reconciliation: the backend recognizes the same object. Lost acknowledgement
likewise repeats evidence transfer, never a download/install. These broker objects
are audit-linked private evidence; this slice does **not** create normal worker
output manifests/catalog entries, automatically release them, expose them in the
artifact browser, or add retention deletion scheduling. Retain the journal and
project object store together; worker journals are not included in control backups.

On exclusive reopen, unfinished operations gain an `unknown` recovery receipt.
Recover their original signed package with `journal.retained_package(operation_id)`.
Cancellation is durable desired state; cancelled is recorded only after confirmed
stop. Lost replies remain unknown even when later stop is confirmed. There is no
automatic restart, re-effect, refund, deletion or unknown-slot release. A future
operator reconciliation mechanism must establish real termination before freeing
an unknown slot; do not delete the journal or invent a new operation to bypass it.

## Opt-in local/Kubernetes configuration contract (not activation)

`RealBackendConfig` is strict/frozen, accepts only these fields, and defaults
`enabled` to false:

```json
{
  "enabled": false,
  "target": "kubernetes",
  "sandbox_runner_id": "deployment-owned-runner",
  "network_client_id": "deployment-owned-pinned-tls-client",
  "isolation_inspector_id": "deployment-owned-runtime-inspector"
}
```

`target` may also be `local`, never Salad. IDs refer to an explicitly configured
trusted registry, not executable names, paths, command lines or agent-supplied JSON
objects. No environment switch auto-loads or activates this configuration. Even
opting in and injecting objects currently returns blocked. Later activation needs:

- Independent inspection of non-root/read-only image, mount namespace, no host,
  control-PVC, Docker socket, host network/PID/IPC, privilege or ambient credentials.
- Broker-only enforced egress; actual DNS/IP pinning and TLS, network policy and
  no ambient proxies. A declaration or self-asserted `FenceEvidence` is insufficient.
- Quota-enforced isolated target, no-follow directory-FD writes, no archive/hardlink
  escapes, offline hook-free package verification and full dependency closure.
- A separately supervised watchdog/kill/confirmed-termination path. The synchronous
  fake interface cannot preempt a malicious or hung Python callback; a timeout
  parameter is not proof of real wall-clock enforcement.
- Authenticated remote package/check/claim/reconciliation channels, signing-key
  lifecycle and persistent single-owner journals. Do not mount control state in a
  worker to reuse the in-process fixture.

## Offline verification

```sh
.venv/bin/pytest -q tests/test_runtime_capability_broker.py tests/test_runtime_capability_journal.py tests/test_capabilities.py tests/test_capability_fencing.py tests/test_capability_contract.py
```

Tests use temporary state and injected fixture effects, including fault injection,
authority mutation, crash/reopen, duplicate journals and sink rollback. They are
not live isolation, Kubernetes activation, provider accounting or scientific evidence.
