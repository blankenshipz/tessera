# Direct Agent Sandbox adapter: operator gates and authenticated commands

This is a **one-shot, network-none** integration with upstream Agent Sandbox
v1.0.6, commit `cd0761d5001baaf293460cd382ee318744d6b249` (Apache-2.0).
[Decision 0030](decisions/0030-direct-agent-sandbox.md) specifies the boundary.
Default tests use synthetic Kubernetes effects. No live Sandbox, network
isolation, runtime isolation or reasoning-model journey has been verified here.

## Operator prerequisites (not installed by Tessera)

1. Explicit kubeconfig reference/digest, context, HTTPS origin/server digest,
   CA digest, `kube-system` cluster UID and dedicated namespace UID. Namespace
   ownership must be bound to this organization/project, not shared tenant state.
   There is no current-context, environment credential or Keychain fallback.
2. Authentic pinned upstream controller and v1beta1 Sandbox CRD. Record Deployment
   UID, digest image and CRD UID/spec checksum in the reviewed target. A configured
   version is not proof of the installed controller build.
3. Strong RuntimeClass with exact UID and actual `runsc` or approved Kata handler;
   plain runc, absent runtimes and `available=true` are not isolation evidence.
4. CNI-enforced default-deny **ingress and egress**, including DNS. Install/read
   back the policies before Sandbox creation. Every selecting additive policy is
   examined before release; any nonempty ingress/egress rule rejects network-none.
5. Exclusive operator/controller/Tessera namespace writers and admission that
   rejects profile drift, adopted/misowned resources and unauthorized gate
   removal. Upstream has name-based child deletion paths: UID checks in Tessera
   alone do not eliminate another Kubernetes writer's race. Freeze this policy
   while an operation is admitted.
6. A trusted verifier of independent, authenticated measured runtime/CNI/admission
   evidence. Reuse the [runtime witness trust/custody boundary](runtime-witnesses.md)
   and [target adapters](runtime-witness-targets.md); generic Job probes must not
   be relabeled as Sandbox-specific admission/replacement proof. The verifier
   must bind its measured receipt to the approved witness identity, target,
   tenant/project, exact policy set and fixed profile; validate native raw proof,
   authentication and freshness. An operator JSON assertion or successful dry
   run is not such a verifier. Installing/provisioning witnesses or controllers
   requires separate authorization.
7. A retained storage class, appropriate quota/admission, and namespaced projected
   API credential/CA references. Do not grant workload containers API tokens or
   namespace write permissions. Operators must additionally constrain the
   controller's scope and deny incompatible Sandbox/template mutation.

The normal server composes the authenticated HTTPS client and supported signed
witness-envelope verifier from an explicitly digest-approved mode-0600 operator
configuration. No operator-written Python factory is required:

```sh
tessera serve --state-dir /private/STATE --port 8765 \
  --agent-sandbox-config /private/OPERATOR-CONFIG \
  --agent-sandbox-sha256 APPROVED-CONFIG-DIGEST
```

The JSON configuration follows `SandboxOperatorConfig`: `target` contains all
`SandboxTarget` fields and exactly matches the approved plan; `ca_reference` and
`token_reference` are absolute private references, not credential values.
`witnesses` has exactly `cni`, `quota`, `watchdog`, `admission`, each with absolute
`report_reference`, approved `report_sha256`, private `authentication_reference`
and the independent service's `WitnessPin`. An optional digest-pinned
`collector_image` authorizes the installed Tessera read-only collection image.
The initial API address is a certificate-valid literal IP: DNS resolution cannot
consume the short final authority-writer budget. HTTPS verification and no-proxy
transport are retained. Missing/mismatched configuration fails before serving or
provider writes; without configuration, `tessera serve` stays provider-disabled.

Reports use the existing `seal_report`/`open_report` authenticated witness custody
envelope, distinct identities and keys, and `source=independent-daemon`. They must
bind tenant/project/namespace, the direct-Sandbox fixed profile, upstream commit
and `scope_digest(target)` (excluding only the receipt digest to avoid circular
hashing). Each kind supplies all `CASES` from `agent_sandbox_configuration.py` in
`native.sandbox_observations`, with attributed subject/control resource UIDs,
Node UID, observation time and structured native request/response receipts.
The verifier checks positive/restricted CNI, quota, runtime/termination and
admission observations, not boolean enable/assertion files. Authentication does
not prove sensor independence: provisioning the trusted witness services and
their real measurements remains an operator prerequisite, not something this
task claims to have deployed or exercised. Reports expire after five minutes.

`verify_witness(target)` must return the **verified** canonical measured receipt,
not a raw untrusted request. The approved `witness_sha256` pins it, with a maximum
age of 15 minutes at the backend boundary (five for normal signed composition).
Required receipt fields are `observed_at`, `org_id`,
`project_id`, `cluster_uid`, `namespace_uid`, `runtime_class_uid`,
`runtime_handler`, and these verified summaries of underlying raw receipts:

```
network: ingress-egress-denied-with-positive-controls
runtime: independently-measured-isolation
admission: exclusive-writers-fixed-profile-one-uid-release
```

These summary strings are not themselves evidence; the trusted verifier is the
security boundary. Without that verifier, creation is unclaimed and rejected.
The isolated tests explicitly inject a synthetic verifier and do not establish
these live guarantees. Refreshing receipt/target pins requires new plan review,
not silently replacing a frozen approved target.

## Plan and normal authority workflow

Use the existing authenticated project/task plan, human approval, execution
limits, registered ready worker, snapshot release, execution grant and lease.
Select `execution_spec.profile = "agent-sandbox"`, `network_enabled = false`,
with `sandbox` containing `target` (all `SandboxTarget` fields),
`cpu_millicores`, `memory_bytes`, `storage_class`, `storage_bytes`, `network=none`.
The existing grant carries the digest-pinned workload image. No mutable template,
sidecar, host mount or arbitrary Kubernetes body is accepted.

Schema 56 is additive. Stop the service and back up private state before the
explicit `tessera migrate --state-dir ...` upgrade. Migration creates no target,
authority, credential or provider effects and invalidates active pre-upgrade
backup freshness while retaining held/failure history. Backups preserve release
fences; restored control state remains held for existing independent recovery.

## CLI/API lifecycle

Credentials below are existing owner-bound private mode-0600 files containing a
bearer value, **not** command-line values or copied keys. Worker and human files
are distinct. All commands use the authenticated control API.

```sh
tessera agent-sandbox create --control-url https://CONTROL \
  --credential-file /private/WORKER-REFERENCE --lease-id LEASE \
  --epoch EPOCH --idempotency-key CREATE
tessera agent-sandbox run --control-url https://CONTROL \
  --credential-file /private/WORKER-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key RUN
tessera agent-sandbox release --control-url https://CONTROL \
  --credential-file /private/WORKER-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key RELEASE
tessera agent-sandbox status --control-url https://CONTROL \
  --credential-file /private/HUMAN-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key OBSERVATION
tessera agent-sandbox stop --control-url https://CONTROL \
  --credential-file /private/HUMAN-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key STOP
tessera agent-sandbox collect --control-url https://CONTROL \
  --credential-file /private/HUMAN-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key COLLECTION
tessera agent-sandbox cleanup --control-url https://CONTROL \
  --credential-file /private/HUMAN-REFERENCE --operation-id OPERATION \
  --manifest-id MANIFEST --manifest-digest DIGEST --idempotency-key CLEANUP
```

Create returns the existing external operation ID and suspended Sandbox UID.
Run requests a still-gated Pod. Wait for the controller's gated Pod before the
separate release command. The release fence is one-use even on timeout/crash;
never automatically release a replacement. Initial lease expiry is frozen as
upstream Retain expiry; lease renewal cannot extend this initial profile.
The fixed Pod explicitly pins `priority=0` and
`preemptionPolicy=PreemptLowerPriority`; elevated priority, PriorityClasses,
preemption mutation and sidecars are rejected rather than broadly stripped as
admission defaults. Read-only discovery occurs outside the control writer with
a ten-second total monotonic budget, 64 requests and an 8 MiB snapshot bound.
The final writer section rechecks live Tessera authority immediately before the
effect, fresh namespace/resource identity and exact spec, and uses a two-second
total transport budget with resourceVersion/UID fencing. Revocation can commit
while discovery waits; discovery timeout means unknown, not safe deletion.

`status` makes an observation; use a new key for a new observation. GET
`/agent-sandboxes/{operation}` returns **last recorded**, not live status.
Original expired/revoked execution grants cannot execute or replay execution,
while separately authenticated human stop remains available without renewing
dispatch credentials. Stop is only a desired/acknowledged suspension until a
fresh observation establishes quiescence.

Collect returns a neutral typed handoff plus PVC UID/spec checksum, **not bytes**.
Only fresh observed terminal container evidence on the same healthy Node UID,
   matching suspended generation, complete Pod/PVC discovery and no contradictions
permit that handoff. A running Pod lost on force deletion, 403/410, pagination
failure, unknown node or lost terminal evidence retains the PVC; independent
operator termination recovery is needed. `collect` still means handoff only.
The explicit `deliver` command now connects that handoff to the existing PVC
collector journal, networkless installed-image entrypoint and durable output
delivery journal, then real worker quarantine upload, private project placement
and artifact-manifest APIs. It does not fake a Job or run arbitrary exec:

```sh
tessera agent-sandbox deliver --control-url https://CONTROL \
  --credential-file /private/WORKER-REFERENCE --operation-id OPERATION \
  --epoch EPOCH --idempotency-key DELIVERY \
  --operator-config /private/OPERATOR-CONFIG --operator-sha256 CONFIG-DIGEST \
  --package-file /private/FROZEN-PACKAGE --custody-root /private/CUSTODY
```

The package is the already admitted private runtime package, not a new execution
grant; custody is an existing owner-private directory. This command creates a
separate digest-pinned, strong-runtime, read-only PVC collector Pod after fresh
authenticated stopped-Sandbox evidence. Its distinct output-operation label must
also select ingress/egress deny policies. Admission must enforce this collector
profile. Exact collector UID/spec and terminal container/healthy-Node evidence
are checked before retaining bytes. Immutable byte custody permits transfer
retry; no automatic producer restart, execution renewal or public release occurs.
Collector Pods and private journals are retained, not automatically deleted under
uncertain termination. Lost creation UID, mutated reader, missing output, checksum
failure or unknown termination fails closed. Uncertain/missing transfer or
placement prevents cleanup. This command's cluster writes require authorization;
only synthetic Kubernetes/local executable byte-transfer tests ran in this task.

Cleanup separately verifies that exact manifest, uploaded artifact identities,
active project placement/retention and actual checksummed project bytes before
UID/resourceVersion-preconditioned PVC/Sandbox deletion. A private placed object
does not require public release. Delete acknowledgement is not observed object
absence. Lost cleanup effects require explicit UID reconciliation, not automatic
new-object deletion; logout/session close performs no cleanup.

## Deliberate limits and remaining live gates

This adapter does not transfer snapshot files, deliver reasoning credentials,
enable provider/network access or run a persistent coding session. The explicit
PVC byte-delivery command is implemented, but no live PVC collection was verified.
Those limits are not represented as successful runtime behavior. Use a
prebuilt pinned networkless workload that needs no input transfer for the initial
minimal journey; do not claim a general network-enabled agent works.

Read-only prerequisite inspection of the explicit `docker-desktop` context found
the API reachable (`kube-system` UID `3d6e6231-8948-470e-86fa-bdf8dab649e3`),
`sandboxes.agents.x-k8s.io` CRD absent, and no RuntimeClasses. No controller was
installed, cluster resource changed, credential copied or provider invoked.
Live gates remain: authentic pinned controller/CRD, enforced CNI and RuntimeClass
measurement, Sandbox-specific admission/exclusive-writer witnesses, real Pod/PVC
lifecycle/replacement/termination proof and governed byte delivery. Default
local tests cannot satisfy those gates.
