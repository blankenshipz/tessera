# Independent runtime witness boundary and runbook

## Status — do not confuse integration with target support

Implemented and tested with **synthetic transports and observations**:

* Four mandatory CNI, quota, external-watchdog and admission report validators.
* Fresh authenticated challenge/RPC, separate witness identities, daemon execution
  core and explicit TLS server entrypoint, one-use durable custody, fail-closed
  collector/activation integration, and fixed bounded daemon-side probe programs.
* An opt-in conformance client and strict configuration schema. Default execution
  does not read configuration/credentials, open a socket, or create any resources.

**Not a complete deployable live witness implementation:** the `NodeSensors`
interface still needs a reviewed implementation for the target's CNI telemetry,
CRI/cgroup mapping, quota-enabled filesystem/CSI driver and external watchdog.
The [target adapter layer](runtime-witness-targets.md) now implements Docker Desktop
preflight, native authenticated RPC/decoding, real API admission and Linux/watchdog
primitives. It still needs a complete deployed native driver for CNI flow/CRI/mount
mapping, fixture supervision and project-quota queries; it is not a built-in
Cilium/Calico/CSI driver or validated Docker Desktop conformance implementation.
`UnavailableNodeSensors` has no effects and returns unknown through each program.
There is no supplied/validated fixture image, Kubernetes defaulting compatibility
profile, automatically installed daemon, or node privilege/RBAC bootstrap.
The HTTPS server entrypoint is a deployment library function, not an automatically
started service. Consequently a clean checkout cannot demonstrate live enforcement.

No real cluster/API/Pod/CNI/kernel/quota/watchdog effect was run for this change.
Real broker effects and real runner clients remain disabled. Neither signed
observations nor a conformance exit code issues an execution grant.
Target-specific conformance uses explicit `--live --target` and mTLS/profile pins;
see the [target runbook](runtime-witness-targets.md). Legacy protocol reports without
a target profile cannot authorize non-fake activation.

## Trust boundary

The collector, separately supervised witness services, their platform-specific
node sensors, approved fixture image and cluster/node administrators are trusted.
The workload process, broker process, agents, uploaded reports and arbitrary
operator-supplied outcome JSON are **not** witnesses. The witness API accepts only
a generated challenge for its one deployment-approved `RunnerPlan`; it has no
report-import, arbitrary command, arbitrary URL, exec-plugin or shell endpoint.

Deploy witnesses outside the workload **and control** namespaces, under four
distinct principals. Keep collector, dispatcher, workload and watchdog credentials
separate. A witness ServiceAccount must not be the broker/dispatcher or workload
account. The watchdog must also be a separately scheduled process whose own clock
and termination authority survive broker suspension. Calling a timer in the broker
does not meet this boundary. Each Pod observation includes its node UID and
ServiceAccount identity; reports pin observer namespace UID, target node UID and
clock boot identity. Administrators must ensure the independent scheduler and
sensor mappings actually meet these assumptions; names/signatures cannot prove it.

RPC uses the existing bounded numeric-IP HTTPS transport: explicit CA, IP SAN
verification, no proxy, redirects, ambient CA/kubeconfig context, DNS discovery,
credential plugin or retry. Each observer has its own bearer, request-HMAC key and
report-HMAC key. The eight request/report keys must be distinct and must not equal
the collector signing key. Keep them in private existing runtime files; no key or
credential is generated or materialized by conformance. HTTPS responses include a
bounded JSON body and `Audit-Id`; chunking/compression are not supported.

Request domain: `tessera-witness-challenge-v1` plus NUL. Report domain:
`tessera-runtime-witness-v1` plus NUL. Payloads are canonical sorted compact JSON
with no NaNs. Reports carry their SHA-256 digest and HMAC-SHA256 signature.
HMAC authenticates trusted custody, **not** an independently verifiable signature
against a malicious collector: the verifier possesses the MAC key. Compromise of
an observer's signing key or privileged sensor defeats that observer's evidence.

## Binding, freshness and durable consumption

Every request/report carries a fresh unpredictable collection challenge plus the
unique manifest run nonce, manifest/plan/package digests, operation, organization,
project, cluster digest and workload namespace UID. It includes exact probe inputs,
raw observations and outputs, created resource UIDs/resourceVersions, timestamps,
deadline, monotonic duration, witness identity and cleanup records. Reports must
agree with separately collected API policy UIDs/versions and admission readback.

The collector generates the challenge; callers cannot select a past one. Four
requests run concurrently inside the existing **five-second** collection window.
The manifest window is not extended. Per-call socket deadlines, at most four
daemon RPCs, 2 MiB per report and zero retries apply. The existing API budget
remains separate (at most 256 catalogue calls). Each node adapter must additionally
bound its own API/CRI/sensor operations and have an external hard deadline. Do not
rely on cooperative Python deadlines to interrupt a hung kernel call or adapter.
The thread executor is not a kill boundary for an incorrectly blocking adapter;
production RPC must use the supplied bounded HTTPS transport and external services.

`witness_attempts` burns nonce/operation/package/plan/challenge before effects or
authority checks. `witness_reports` retains authenticated reports, including
unknown/failed reports. Both are additive append-only worker-journal tables, not
a control-store schema migration. Collector attempts and activation attempts are
also consumed before validation. Crashes, lost replies and revoked authority do
not free an attempt. Restart cannot import historical signed reports as fresh
collection. Revalidation at use checks signatures and freshness again; activation
checks current package authority before **and after** collection.

Missing, failed, unknown, stale, partial, reused, contradictory or mismatched
evidence in **any** gate blocks the whole activation. Other gates cannot vote it
through. The legacy offline `CandidateVerifier` remains diagnostics only; it is
not an alternative live activation path.

## Probe programs and required sensor behavior

`capability_witness_probes.py` defines the fixed programs and the `NodeSensors`
adapter interface. The deployment supplies the adapter in trusted Python
composition, never by a plugin/command field in an incoming JSON request.

### CNI

The program requests distinct disposable identities and fresh endpoints for HTTP
allow, DNS allow and public/private/metadata/control/DNS-bypass/proxy deny cases.
Each endpoint must be independently confirmed live with the fresh challenge.
Only fixture-owned endpoints are permitted: never probe a real metadata service,
control service, public host or external proxy. A target adapter must implement
safe representative routing/classification and prove equivalence to the protected
policy paths; if it cannot, that class is unknown rather than silently relabeled.

The observer records direct connection outcome, HTTP status or DNS response,
challenge output, policy convergence, per-flow CNI verdict and sent/dropped packet
counts. No proxy environment or proxy use is permitted. A denied result requires
the exact policy's independently observed DROP for every sent packet; timeout
alone, refusal, HTTP 403/200, DNS NXDOMAIN or absent telemetry is not a deny pass.
Native CNI events must be correlated to the exact source Pod/identity and fresh
destination, not to aggregate counters or another Pod's dropped traffic.

### Kernel/cgroup/filesystem/PID quotas

The program reads node-side cgroup v2 snapshots before and after bounded fixture
pressure, uses filesystem quota sentinels, checks namespace access and independently
reads termination/events. Bounds are memory limit + 1 MiB, PID limit + one fork
attempt, 250 ms CPU pressure, and output/ephemeral bytes and output inodes + one.
The adapter must isolate pressure in disposable children, account existing usage,
stop at the bound, and never pressure the host or reuse research output. Prepare
must refuse a nonempty/unowned fixture/PVC or unsupported accounting mechanism.

Validators require actual `cpu.max`, `memory.max`, `pids.max`, increased throttling,
OOM-kill and PID-max counters, observed peaks/attempts, exit 137/OOMKilled with no
restart, and consistent events. Filesystems need an observed project hard limit,
mount/project identity, exact exhaustion usage, `EDQUOT`, and unchanged post-write
usage. The output mount must map to the manifest PVC UID. Root/parent/symlink escape,
host/control-PVC and token-path access probes must all fail as specified.

Requested limits, PVC capacity, ResourceQuota, emptyDir sizeLimit, `ENOSPC` on a
shared disk, installer counters and object presence are not hard-quota evidence.
Docker Desktop/local-path cannot pass without independent actual quota evidence.
No host filesystem quota, privileged mount or dependency is installed by this code.

### External watchdog

Two harmless write-sentinel fixtures are armed with an authenticated heartbeat
challenge before broker-suspension and revocation faults. The separate watchdog
must use its own clock/boot identity and issue one kill/revocation only. The node
observer must confirm exit, empty cgroup, credential revocation and an unchanged
write count across a post-termination quiet interval of at least one heartbeat.
Broker-loss kill cannot precede the observed lease expiry. Termination must fit
lease + grace and the overall collection deadline. A delete acknowledgement,
API absence, dead broker, lost heartbeat/reply, observer crash or continued writes
never establishes termination. There is no automatic kill retry.

The narrow five-second profile may be impossible for some configured lease/quiet
windows or slow targets. Such a target blocks; do not widen freshness automatically.
The existing `LeaseWatchdog` protocol core is not itself a node kill/observe adapter.

### Admission/defaulting

The observer retains raw strict server-side dry-run requests/responses for positive
and all forbidden **Pod and Job** controls, and actual disposable positive create
and persisted readbacks. Negative dangerous controls are dry-run only; a broken
policy must not result in executing a privileged/hostPath/Secret-bearing Pod.
Policy/binding readback requires Fail/Deny, warning-free observed generation and
known UID/resourceVersion. Generic RBAC/quota/schema denials do not prove the
selected admission policy denied the forbidden field.

Actual positives require a deployment-owned reviewed harmless-image allowlist in
`WitnessDaemon`, an already-present digest-pinned image and zero pull/retry. The
Job remains suspended; any actual Pod must run only the reviewed fixture, never
a download-capable installer or research command. Missing fixtures block preparation.
Requested/security/image/command/ServiceAccount/volume/resource fields are compared
without wildcard default removal. Unknown fields/defaults/mutation, generation or
UID/version mismatch fail. **No native Kubernetes defaulting profile is currently
approved**, so ordinary native Job/Pod defaults may block this implementation.
Do not strip defaults to manufacture a passing receipt.

## Cleanup, retention and secrets

Adapters journal each exact successful create's UID before using it. Cleanup uses
only those handles, a fresh exact read, full nonce ownership, UID/resourceVersion
delete preconditions and an independent NotFound readback. Never enumerate/delete
by prefix, namespace, label selector or a received report's resource list. The
`cleanup_owned` helper supports the exact workload scope and explicitly supplied
observer namespace pin. Unknown cleanup blocks and is retained for reconciliation;
it does not authorize retry, deleting a PVC or adopting an existing resource.

Keep the complete private worker journal, raw API records, signed witness envelopes,
approved plan, deployment identity/image record and node-native sensor logs in
quarantined deployment custody. No automatic deletion, upload, publication or model
context transfer is performed. Witness evidence is operational, not a scientific
result or control-plane execution receipt. Current execution/audit/journal authority
and reconciliation contracts remain unchanged.

Credentials, CA/private key contents, bearer headers and sensitive response echoes
must not enter reports. Known transport/key bytes and suspicious secret fields are
rejected **before** retention; error text is fixed-category only. This filtering
cannot discover arbitrary secrets encoded under arbitrary names. Use credential-free
synthetic fixtures, sanitize at the node API boundary, restrict access to retained
topology/tenant data, and never send raw secrets to the witness programs.

## Explicit conformance command (not run live here)

```sh
# Zero effects, even with --config supplied:
.venv/bin/python scripts/check_capability_runtime_witnesses.py

# Offline authoritative schema; no config or credential read:
.venv/bin/python scripts/check_capability_runtime_witnesses.py --witness-schema

# Only after separate deployment, adapter validation and authorization of ALL effects:
.venv/bin/python scripts/check_capability_runtime_witnesses.py --witness-live \
  --config /absolute/private/runtime-witness-config.json
```

`RuntimeWitnessConformanceConfig` extends the collector config with
`harmless_fixture: "nonce-owned-disposable-v1"` and exactly `cni`, `quota`,
`watchdog`, `admission` entries in `witnesses`. Each entry has `pin`, `channel`
(explicit credential reference/HTTPS binding for that daemon), absolute
`request_key_file` and `report_key_file` references. Pins name the kind, witness
identity/namespace/namespace UID, node UID and boot ID. Channel principal/scope must
match the pin and its plan digest must match the exact approved plan. Use the
schema rather than pasting credentials into a template. No cloud credentials,
image/model/data download or installation is part of this command.

The API-only `--collect-live` flag does **not** authorize witness effects. The
witness client does not load fake implementations or report files. Fake witnesses
are code-only test injection, and collector composition must explicitly set
`allow_fake_witnesses=True`; those records are labeled `fake`. Production CLI never
sets that option. A passing witness command means fresh observations were retained,
not that real runner support or execution authority has been enabled.

## Revocation and rollback

Disable new witness collection and activation first. Revoke through existing
control authority, independently terminate/reconcile any previously running owned
fixture, and preserve all journals. Do not delete failed reports, recreate a nonce,
re-sign old evidence, weaken Fail/Deny, strip mutations, or reinterpret a lost kill
reply as stopped. Rotate/revoke compromised observer credentials through the
deployment's separately authorized process; this command does not change credentials.
Return to the provider-disabled path without claiming enforcement from API objects.
