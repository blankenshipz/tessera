# Authenticated capability evidence collection

**Implemented and fake-tested; no live Kubernetes API or workload effect was run.**
Collection is a precondition, not proof of continuous enforcement, successful
installation, or execution authority. Provider-disabled defaults and the runner's
real-effect guard remain unchanged.

## Boundaries

`capability_kube_client.py` provides an explicit `ClientBinding`, credential loader,
bounded authenticated client and optional stdlib HTTPS transport. No dependency
installation, kubectl, cloud credential plugin, environment discovery, ambient
context, proxy, system CA, redirect, decompression or automatic retry is used.

Bindings pin the numeric HTTPS API address/port, SHA-256 of the **exact CA PEM
bytes**, cluster identity, namespace name/UID, tenant/project, approved plan digest,
principal and named PV. Kubernetes has no universal cluster-UID endpoint: this
profile uses the explicitly approved `kube-system` Namespace UID and defines the
manifest's `cluster_digest` as `canonical_digest([server, ca_sha256, cluster_uid])`.
Cloning that namespace identity/CA is outside the collector's threat model; cluster
administrators remain trusted. This definition is not interchangeable with other
Tessera target adapters' cluster digests.

Credential modes:

* `kubeconfig`: an explicit absolute file and context, with exactly one selected
  context, cluster and user. The selected context must name its namespace. The
  selected cluster supports only `server` and inline `certificate-authority-data`;
  the user supports only a bearer `token`. `current-context` is **ignored**, not a
  fallback. Exec/auth-provider plugins, insecure TLS, alternate TLS names, proxies,
  client certificates and relative CA paths are rejected.
* `service-account`: explicit absolute token and CA file references and the full
  `system:serviceaccount:<execution-namespace>:<name>` identity. No default mounted
  token path or Kubernetes environment variables are read. References must be
  owner-owned, regular, non-symlink final files and not group/world writable. For
  projected mounts, provision an explicitly reviewed resolved file reference;
  the loader does not follow the usual projected-token symlink automatically.

Tokens and CA/certificate contents have no repr, log or receipt fields. Sensitive
response echoes are rejected before retention, including JSON-escaped bearer
values. Errors expose only `auth-denied`, `timeout`, `malformed-response`, `stale`,
`contradictory`, or `unavailable`. A refused or incomplete request is never a pass.
Rejected sensitive/malformed response bodies are deliberately not retained.

## What is collected and checked

The client constructs its request catalogue from the validated exact plan, not
caller-provided paths. Three explicit credentials are required: independent
collector, dispatcher, and workload. No impersonation headers or TokenRequest
credential minting are used. Each principal proves its identity with a
SelfSubjectReview. The collector cannot borrow dispatcher/watchdog/workload identity.

API records retain decoded raw JSON bodies, method/path/request body, HTTP status,
API audit/request ID, authenticated principal, observation time and request/response
digests. They include:

* Named namespace, ServiceAccount, ResourceQuota, LimitRange, NetworkPolicy,
  Roles/RoleBindings, ValidatingAdmissionPolicies and bindings; exact labels,
  pod-security settings, policy readiness, generation, status and specifications.
* Exact PVC/PV, claim ownership and storage/quota observations.
* Complete bounded Deployment/Pod/Job/Event and namespace Role/RoleBinding lists.
  This is a **pre-next-Job** profile: workload and event lists must be empty.
  Unexpected objects, continuation pages, unknown quota use and mutation fail.
* Dispatcher server-side dry-run Job **and Pod** positive/negative challenges.
  No dry-run result becomes an actual create. Positive admission must preserve the
  exact image, service account, command/args, mounts, quotas and security contexts.
* Dispatcher positive create review and both identities' forbidden-action SSARs.
  Token issuance/exec reviews correctly separate `resource` and `subresource`.
  False/false, missing, evaluation-error and contradictory statuses fail closed.

Every named prerequisite, PVC, PV and cluster namespace requires an explicit
baseline `[uid, resourceVersion]` pin. These pins are compared to API readback,
**not accepted as evidence themselves**. UIDs, versions and creation times must be
present. Objects and namespace inventories are re-read at the end; any drift,
including same-version changed content, fails. Versions are opaque strings, not
sortable clocks. Namespace/SA/PVC UIDs additionally match the activation manifest.
No collection protocol can make these reads atomic with a later create; use-time
authority, admission and watchdog checks remain mandatory.

### Runtime witness and honest limits

API readback cannot prove CNI packet drops, installed image identity, TLS behavior,
kernel `EDQUOT`, cgroup limits, watchdog heartbeats or actual termination. A
supervisor-injected `RuntimeWitness.observe` must obtain the existing inspector's
raw, nonce/plan-bound challenge receipts for those checks, including negative RBAC
controls attributed to both identities. Collector API SSAR results must agree with
those negative-control receipts. Neither an operator-signed claims file nor a
`passed` boolean can substitute. There is **no claims-file witness loader**.

The all-pass default tests use an explicitly fake API and fake independent witness.
The [runtime witness integration](runtime-witnesses.md) now supplies authenticated
daemon RPC, four mandatory report validators and fixed probe programs. Its
target-specific CNI/kernel/CRI/filesystem sensor adapters and reviewed fixture image
are still deployment gaps, not inferred capabilities. The API-only CLI retains partial API observations and
exits blocked even if every API check succeeds. It never invents runtime evidence,
performs dangerous exec/token negative effects, or calls real network/storage probes.
The external witness deployment must be reviewed and independently tested before
any real activation; merely injecting a Python object is not proof of independence.

The existing raw inspector remains intentionally strict: unreviewed Kubernetes
defaulting, different native admission error/status shapes, quantity spellings,
CSI fields, or additional fields do not get normalized into a pass. This is not a
general Kubernetes compatibility guarantee or exhaustive cluster-wide RBAC audit.

## Authentication, freshness and one-time activation

`KubernetesEvidenceCollector` canonicalizes retained evidence with sorted compact
JSON and signs the domain `tessera-collector-v1` plus NUL plus payload using an explicitly supplied
collector HMAC key of at least 32 bytes. Keep that key separate from approval,
dispatcher, workload and watchdog credentials. The authenticated payload binds
collector/server/CA/cluster, manifest and plan digests, tenant/project, package,
operation, nonce, start/completion and manifest expiry. HMAC proves custody, **not
independence or execution**. Retain the raw record bundle, not only its digest.

The private owner-locked worker journal retains additive `collector_attempts`,
`collector_evidence` and `collector_consumed` tables. Operation/package/plan/nonce
are burned **before** inspection. Failures retain classified partial evidence;
crashes retain the attempt and cannot retry. Tables cannot be updated/deleted.
Successful envelopes additionally carry a local issuer seal: even a valid imported
signature is not an eligible live result. Use consumes the result before checking
it; mismatched manifests, tampering, failure or replay cannot repair it in place.
Restart makes historical envelopes ineligible rather than recreating authority.

`RawEvidenceInspector` unwraps and revalidates authenticated results. The
`CollectorActivationGate` burns its activation attempt before current package and
collector checks, and rechecks package authority afterward. Its return is only
manifest/evidence identifiers—not a grant or transferable execution token. The
fake runner can compose `evidence_collector=` before its existing global-claim,
suspended-create and watchdog sequence. Real runner clients remain rejected.

Each observation must fall inside the manifest window and this collection, and be
at most five seconds old. The collector's total API/witness deadline is at most
five seconds; each client also has a 1–5 second request timeout and 1–120 second
total lifetime. At most 256 calls overall, zero retries, 2 MiB per response, 100
items per list (pagination rejected), 8 MiB API-record budget and 2 MiB runtime
bundle are allowed. Deployments must satisfy those bounds, not extend freshness to
accommodate a slow cluster. OS socket deadlines are not an external hard watchdog.

## Opt-in API-only runbook

Do **not** invoke the live mode without separate authorization for the exact
target and server-side dry-run/review calls. It can cause API audit records and
dry-run admission webhook processing, but never persisted workload creates.

```sh
# Disabled: does not open config/credentials or contact Kubernetes.
.venv/bin/python scripts/check_capability_kubernetes_collector.py

# Offline, authoritative schema; no credentials or network.
.venv/bin/python scripts/check_capability_kubernetes_collector.py --schema

# Separately authorized API collection only. Currently exits 2: no runtime witness.
.venv/bin/python scripts/check_capability_kubernetes_collector.py --collect-live \
  --config /absolute/private/collector-config.json
```

`CollectorConformanceConfig` requires `enabled: true`, the full approved `plan`,
`collector`/`dispatcher`/`workload` credential references (each with its complete
`ClientBinding`), `resource_pins`, absolute private `journal`, and an existing
`signing_key_file`. Unknown fields are forbidden. Request budgets across the three
clients must sum to at most 256; choose e.g. 96/96/32, with retries zero. Configure
baseline pins under `object-0` through `object-N` in `plan.objects()` order, plus
`pvc`, `pv`, and `cluster`. Preserve the reviewed plan and baseline identity record.
Do not generate new credentials, acquire cloud access, or install prerequisites as
a side effect of configuring the collector. The CLI does none of those things.

## Retention, revocation and rollback

Keep the entire private journal in deployment-owned quarantined custody. Evidence
can contain operational topology and tenant identifiers; do not publish it or
attach it to model prompts. Retain failed attempts, raw accepted responses and
signatures with the approved plan and API audit records. No automatic expiration,
deletion, upload, control-store migration or artifact release is implemented.

On revocation, stop admitting new manifests and revoke through existing control
authority. The gate rechecks current authority; the runner still performs use-time
checks and watchdog fencing. Expiry/failure/revocation never frees a nonce. A new
attempt requires newly approved exact authority, not re-signing old observations.
Collection alone starts nothing and cannot stop an existing workload. Roll back
by disabling collection/activation and preserving journals; separately reconcile
any already-running Job through the verified watchdog/termination path. Do not
delete PVCs, unknown Jobs or nonce records, weaken policy, or turn a delete reply
into stop evidence. No live enforcement is claimed by this runbook.
