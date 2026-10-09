# Opt-in two-variant remote compute pilot

**Live execution was not run for this change.** No explicit pilot configuration,
named authorized remote target, runtime credential bindings, or target cost
enforcement record was supplied. Running `--run` without `--config` was observed
to return `blocked`, `live_ran: false`, `blocker: "--config required"` (exit 1).
The default invocation returned `not_started`, `live_ran: false` (exit 0).
Do not interpret offline fake effects as live compatibility evidence.

## Scope, not a model-provider activation

`scripts/check_comparison_pilot.py` implements one opt-in **Kubernetes compute
provider** pilot through the existing baseline/intervention API workflow. It
uses two already human-approved constant CPU plans, two separately issued grants,
ordinary admitted attempts/packages/dispatches, and
`KubernetesDispatchWorkerService`. It does not use the standalone OAuth fixture's
parallel Job launcher. See [decision 0016](decisions/0016-comparison-compute-pilot.md).

The workload is deliberately only `python -c 'print(1)'` for baseline and
`python -c 'print(2)'` for intervention. `result.txt` is **observed owned-Pod
stdout**, not bytes read from the PVC or fabricated from the expected answer.
The collector verifies exact stdout and uploads those actual bytes into quarantine.
The registered input snapshot is required by the ordinary grant contract but
these constant probes do not consume it; input materialization is not exercised.

This is not an LLM pilot: `provider_enabled=false` remains immutable in the
control package. There are **zero model calls, zero model tokens**, no OpenCode
server, model credential bridge, API-key fallback, top-up, or provider billing
settlement. An LLM/provider-enabled research workflow still needs a separately
implemented scoped broker/profile/adapter. This slice must not be represented as
completion of that capability or a scientific comparison.

## Invocation and already-provisioned prerequisites

No dependencies are installed and no target is provisioned by the harness.
From the repository root, using the existing environment:

```sh
# Default: no credentials read and no network calls.
.venv/bin/python scripts/check_comparison_pilot.py

# Live opt-in; config is an absolute path to a private operator-owned JSON file.
.venv/bin/python scripts/check_comparison_pilot.py --run --config /private/pilot/config.json

# Offline tests exercise both variants and faults, with fake effects only.
.venv/bin/pytest -q tests/test_comparison_pilot.py tests/test_comparison_pilot_remote.py
```

There are **no environment-variable defaults**. In particular, `KUBECONFIG`, the
current context, proxy environment variables, ambient cloud/model credentials,
and coding-agent credentials are not discovered or used. Runtime files are
explicit existing references. The harness never creates credentials or Secrets.

Before invoking live, an operator must already have:

* An ordinary control service on authenticated HTTPS (or literal loopback HTTP),
  a dedicated project and experiment containing exactly these two registered
  immutable variants and **no previous attempts**, human approval of each exact
  plan, and two active grants for the same enrolled project-scoped worker.
  Approval is not a grant. The script performs separate human-authenticated
  admission/dispatch using the existing owner credential; it does not approve or
  edit plans, enroll workers, or increase limits.
* Plans with `profile=kubernetes-opencode`, `network_enabled=false`, no skills,
  `output_paths=["result.txt"]`, output cap 2–4096 bytes, and duration 1–120 seconds.
  Their `argv` must exactly match the constant probes above. The image must be
  digest-pinned, approved, already present on the selected nodes, and actually
  support that command as UID 65532. The existing Job contract uses
  `imagePullPolicy=Never`; missing images are failures, not permission to pull.
* Exactly two project concurrency **reservation** slots and sufficient existing
  internal monetary reservation. The pilot runs only one Job at a time. Both
  grants remain held after execution because unknown cost is not settlement; do
  not lower the reservation slots to one or invent zero settlement to admit the
  intervention. A grant's `max_exposure_cents` is an internal reservation only;
  it is never a Kubernetes, Docker Desktop, or provider billing cap.
* An explicit HTTPS Kubernetes server, pinned CA file, kubeconfig digest/context
  identity, observed `kube-system` namespace UID (cluster identity), target
  namespace UID, and existing PVC UID/spec digest. No kubeconfig content is used
  for authentication or exec plugins; it is a pinned target inventory reference.
* A namespace ServiceAccount credential already authorized for this tenant and
  project: read namespaces `kube-system` and the target; self-subject review;
  read target NetworkPolicy/PVC/Jobs/Pods/logs; create target Jobs; delete exact
  target Jobs. It must have no cross-project writer authority. Least-privilege
  RBAC deployment is an **operator prerequisite**, not something this harness
  installs or fully audits. A Kubernetes SelfSubjectReview verifies the selected
  namespace ServiceAccount identity. The immutable control package's organization
  and project must match the configured credential scope before submit. Runtime
  credential-file changes fence access rather than silently rotate accounts.
* The [dedicated project namespace/admission profile](pilot-workload-isolation.md),
  with `target.isolation` naming the control namespace and approved image,
  `pilot-dispatcher`, `pilot-output` at `/outputs`, and observed identities in the
  derived execution namespace. Shared control namespaces are rejected. The pilot
  re-reads namespace Pod Security labels and exact admission policy/binding/quota
  specs before creation. Named read-only ClusterRole permissions enable cluster
  identity verification; no namespace list or cluster writer permission is needed.
* Fresh target-bound allowed/denied connectivity probe evidence (existing
  `KubernetesNetworkPolicyEvidence` contract, at most 15 minutes old). The two
  probes must name distinct sources and one destination, have unambiguous
  `allowed`/`denied` outcomes, and remain bound to the configured policy,
  cluster, and namespace. Merely having a NetworkPolicy does not prove
  isolation. The script rejects stale or ambiguous evidence and verifies current
  policy/PVC UIDs and canonical spec digests again before creation; it does not
  create probe Pods or attest unobserved connectivity.
* Existing absolute 0700 journal and worker directories; credential files must
  meet the existing `read_private_credential` contract (regular, private, bearer
  token only, **not** `Authorization: Bearer ...`). Retain both directories.

## Exact JSON configuration

Top-level keys are `pilot`, `target`, `runtime`. Replace all illustrative values
with actual authorized identities/evidence. Never put raw secrets in this file.
`target.cluster` and `target.output` use the existing typed Kubernetes contracts:

```json
{
  "pilot": {
    "project_id": "project_REPLACE",
    "experiment_id": "experiment_REPLACE",
    "design_revision": 1,
    "target_name": "selected-remote-cpu",
    "grants": {"baseline": "grant_BASELINE", "intervention": "grant_INTERVENTION"},
    "max_seconds_per_variant": 120,
    "max_output_bytes": 1024,
    "max_internal_reservation_cents_per_variant": 5,
    "cost_enforcement_digest": "REPLACE_WITH_SHA256_OF_COST_FILE_BYTES"
  },
  "target": {
    "cluster": {
      "kubeconfig": "/private/pilot/selected-kubeconfig",
      "kubeconfig_sha256": "REPLACE_WITH_FILE_SHA256",
      "context": "selected-remote",
      "profile": "development-minikube-opencode-v1",
      "server": "https://kubernetes.example:6443",
      "server_sha256": "REPLACE_WITH_SHA256_OF_SERVER_STRING",
      "ca_sha256": "REPLACE_WITH_SHA256_OF_PEM_FILE_BYTES",
      "namespace": "REPLACE_WITH_DERIVED_TESSERA_EXEC_NAMESPACE",
      "cluster_uid": "REPLACE_KUBE_SYSTEM_UID",
      "namespace_uid": "REPLACE_NAMESPACE_UID",
      "network_none_evidence": {
        "capability": "network-policy-enforced-v1",
        "policy_name": "default-deny",
        "policy_uid": "REPLACE_POLICY_UID",
        "policy_spec_sha256": "REPLACE_CANONICAL_SPEC_SHA256",
        "cluster_uid": "REPLACE_KUBE_SYSTEM_UID",
        "namespace_uid": "REPLACE_NAMESPACE_UID",
        "observed_at": "REPLACE_FRESH_ISO8601_WITH_OFFSET",
        "allowed_connectivity": {
          "source_pod_uid": "REPLACE_ALLOWED_CLIENT_UID",
          "destination_pod_uid": "REPLACE_SERVER_UID",
          "outcome": "allowed", "timeout_seconds": 3,
          "observed_at": "REPLACE_FRESH_ISO8601_WITH_OFFSET"
        },
        "denied_connectivity": {
          "source_pod_uid": "REPLACE_DENIED_CLIENT_UID",
          "destination_pod_uid": "REPLACE_SERVER_UID",
          "outcome": "denied", "timeout_seconds": 3,
          "observed_at": "REPLACE_FRESH_ISO8601_WITH_OFFSET"
        }
      }
    },
    "output": {
      "pvc_name": "pilot-output", "mount_path": "/outputs",
      "pvc_uid": "REPLACE_PVC_UID", "pvc_spec_sha256": "REPLACE_CANONICAL_SPEC_SHA256"
    },
    "credential_scope": {
      "org_id": "REPLACE_CONTROL_ORGANIZATION_ID",
      "project_id": "project_REPLACE", "service_account": "pilot-dispatcher",
      "expires_at": "REPLACE_AUTHORIZATION_EXPIRY_WITH_OFFSET"
    },
    "isolation": {
      "control_namespace": "tessera-control",
      "image": "registry.example/pilot@sha256:REPLACE_64_HEX_DIGEST"
    }
  },
  "runtime": {
    "control_url": "https://control.example",
    "owner_credential_file": "/private/pilot/owner.token",
    "worker_credential_file": "/private/pilot/worker.token",
    "compute_credential_file": "/private/pilot/namespace-service-account.token",
    "ca_file": "/private/pilot/cluster-ca.pem",
    "journal_root": "/private/pilot/audit",
    "worker_root": "/private/pilot/worker",
    "cost_enforcement_file": "/private/pilot/cost-enforcement.json",
    "output_storage_enforcement_file": "/private/pilot/output-storage-enforcement.json"
  }
}
```

Canonical spec/target digests use existing `canonical_json` /
`cluster_identity_digest`, not pretty-printed JSON. The CA digest here is the
**exact PEM file bytes**, not a DER certificate fingerprint. Control TLS uses
normal system certificate verification. Kubernetes TLS uses the pinned CA with
hostname verification, no proxies, no redirects and no cookie transport.

`target.isolation` is the input to the separate isolation attestation contract.
Its project/org binding is derived from `credential_scope`, not supplied by the
operator. Before a Job is created the harness reads the dedicated execution
namespace, admission policies/bindings, and resource quota again; it also uses
server-side dry runs to ensure the selected policy rejects an intentionally
invalid Job. This consumption does not provision the namespace, policies, RBAC,
or storage and cannot replace the fresh NetworkPolicy connectivity evidence.

The operator's `cost_enforcement_file` is a retained, independently verified
target provider-billing contract, **not an internal grant/reservation or a
self-issued billing receipt**. It can permit a run only when the provider itself
enforces the zero incremental cap:

```json
{
  "version": 1,
  "target_digest": "REPLACE_EXISTING_CLUSTER_IDENTITY_DIGEST",
  "project_id": "project_REPLACE",
  "currency": "USD",
  "billing_state": "provider_enforced_incremental_cap_v1",
  "max_provider_incremental_cost_cents": 0,
  "autoscaling_disabled": true,
  "expires_at": "REPLACE_FUTURE_ISO8601_WITH_OFFSET"
}
```

For Docker Desktop/local storage, provider billing cannot be established from PVC
objects, filesystem capacity, or a zero-dollar internal grant. Its retained cost
record must instead say `"billing_state": "operator_attested_unknown"`; the
harness deliberately blocks before reading credentials or contacting Kubernetes.
Metered compute, autoscaling, paid model calls, new storage/egress, an operator
attestation alone, or an unavailable provider-enforced cap block live execution.
Incurred usage/cost remain `unknown`; prepaid infrastructure is not labelled free.

The separate `output_storage_enforcement_file` binds the exact PVC name/UID/spec
digest and dedicated labels (`app.kubernetes.io/managed-by=tessera`,
`tessera.io/output-role=dedicated-pilot-output`, and the exact
`tessera.io/project-id`) to the selected target/project.
It may admit a run only with `state=filesystem_quota_enforced_v1`, a non-expired
real byte-exhaustion record and a real inode-exhaustion record (`ENOSPC` or
`EDQUOT`) at or below the declared bounds. A PVC `requests.storage` value, free
space, or a `ResourceQuota` is not a runtime byte/inode write bound. Docker
Desktop's ordinary local-path storage must be recorded as
`state=operator_attested_unknown`; this also blocks live execution. The exact
constant-probe collector additionally rejects stdout over the plan's byte cap,
but that collection check cannot substitute for PVC inode enforcement.

## Bounds, audit and failure semantics

* Two attempts maximum, one per variant; sequential Jobs; no automatic external
  retry. Job `backoffLimit=0`, `restartPolicy=Never`; 500m CPU, 256 MiB memory,
  64 MiB ephemeral storage and no network follow the existing canonical manifest.
* Each workload is at most 120 seconds, further bounded by the control lease and
  hard expiry. The worker renews live authority and uses a 10-second disconnect
  policy. HTTP requests have five-second caller deadlines. Cleanup/reconciliation
  and HTTP overhead are additional to workload duration; there is no claim of a
  240-second end-to-end wall clock. Control report/lease retries reuse durable
  identities; they never imply a second external submission.
* Output is 2 bytes for each exact probe, capped at the approved limit, never over
  4096 bytes. Logs are read without server truncation and rejected over the bound.
  The selected PVC requires separately verified byte **and inode** enforcement;
  the output evidence and current PVC labels/UID/spec are checked before submit.
  Collection requires one owned terminal Pod, zero container restarts, and the
  same Pod UID after reading. Arbitrary scripts/unbounded tools are not accepted.
* The ordinary control ledger retains human approvals/grants, admission,
  immutable package digest, lease/epoch, dispatch and worker observations.
  The existing adapter/worker journals retain Job intent, UID/spec bindings,
  requests, terminal reports and exact replay identity.
* `pilot.sqlite3` is supplemental private audit: one-use config digest, timestamps,
  API request/response digests and idempotency keys, remote request/response
  digests, namespace ServiceAccount identity, submit/cancel intents, observed
  Job/Pod/provider identity, package/artifact/manifest digests, unknown cost/usage,
  and final `human_review=pending`. It contains no bearer values or raw logs.
  `pilot-readiness.sqlite3` is a separate append-only gate ledger. It records
  passed or blocked configuration, cost, storage-attestation, and
  isolation/credential checks with a config digest. A blocked readiness record is
  not an execution claim and never creates or reopens `pilot.sqlite3`.
* The harness reads grant state/expiry during preflight and rechecks it immediately
  before each admission and dispatch. It rechecks the compute credential scope,
  cost-evidence expiry, storage-attestation expiry, policy/PVC bindings, and fresh
  isolation evidence before every Job create. Nothing silently renews an expired
  grant or credential, reloads a changed credential file, or turns stale evidence
  into a submission authorization.
* A lost control reply stops orchestration. Reopening the pilot journal refuses
  submission, even with changed arguments. A new journal cannot replace previous
  experiment attempts. **Do not delete the journal to retry.** A lost remote POST
  is reconciled by the original stable Job name; a 404 before that Job has ever
  been observed remains unknown, including after restart, rather than proving
  cancellation. Unknown/stop/output-limit cases prevent starting intervention.
* Grant revocation is requested through the ordinary API. Worker cancellation
  fences future starts and deletes only the recorded Job UID; an acknowledgement
  is not termination. Replacement UIDs, unavailable observations, and in-flight
  submit uncertainty retain exposure and require manual reconciliation. Kubernetes
  controller/node failures do not offer exactly-once external execution.
* Nothing automatically resolves the human attempt outcome, accepts evidence,
  releases artifacts, reviews a conclusion, reconciles charges, deletes a
  namespace/PVC, or cleans up retained Jobs. After inspecting actual observations,
  a human uses the existing `/resolve`, artifact-link, measurement, conclusion
  and `/review` routes from [the ordinary workflow](local-api-workflow.md).

For cancellation/recovery, retain the same control database and worker root,
revoke the affected grant using its freshly read revision, and resume the
existing `KubernetesDispatchWorkerService` for that exact dispatch/root/target.
This script deliberately does not replay the whole pilot or invent a second
recovery authority. If an uncertain create has never been observed, an operator
must reconcile it at the selected cluster; a fresh pilot is not recovery.

Offline acceptance does not establish target RBAC, isolation, storage quotas,
cost enforcement, real TLS/ServiceAccount compatibility, or laptop independence.
These remain unrun selected-target gates. The harness is experimental, not a
supported remote deployment or a replacement for the model-provider activation
requirements in the compatibility matrix.

## Verification recorded for this implementation

On the existing dirty working tree, with no commits, installs or live effects:

```sh
# 123 passed; two existing Starlette/httpx and anyio deprecation warnings
.venv/bin/pytest -q tests/test_comparison_pilot.py tests/test_comparison_pilot_remote.py tests/test_local_api_workflow.py tests/test_provider_activation.py tests/test_credential_access_leases.py tests/test_worker_channel.py tests/test_execution_contracts.py tests/test_kubernetes_execution.py tests/test_kubernetes_contracts.py

# 66 passed; the same two warnings
.venv/bin/pytest -q -o faulthandler_timeout=30 tests/test_kubernetes_dispatch.py tests/test_kubernetes_dispatch_recovery.py tests/test_provider_disabled_kubernetes_e2e.py

# Passed; all five implementation/test files formatted
.venv/bin/ruff check src/tessera_worker/comparison_pilot.py src/tessera_worker/comparison_pilot_remote.py scripts/check_comparison_pilot.py tests/test_comparison_pilot.py tests/test_comparison_pilot_remote.py
.venv/bin/ruff format --check src/tessera_worker/comparison_pilot.py src/tessera_worker/comparison_pilot_remote.py scripts/check_comparison_pilot.py tests/test_comparison_pilot.py tests/test_comparison_pilot_remote.py
git diff --check
```

The new tests contribute 24 offline cases. An initial combined command named the
nonexistent `tests/test_control_channel.py` and collected no tests; it was corrected
to `tests/test_worker_channel.py` in the passing command above. Initial fixture/API
shape and lint failures were repaired. Full-repository integration/desktop gates
were not run and are not implied by these targeted results.
