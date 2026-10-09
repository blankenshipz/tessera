# Capability sandbox activation: partial implementation, activation blocked

**Current addition:** [Kubernetes runner/installer components and runbook](capability-kubernetes-runner.md)
now cover exact manifests, raw-observation inspection, a durable fake-client
lifecycle and bounded hook-free wheel materialization. Real activation is still
blocked. The candidate/TLS/watchdog milestone and its historical checks/remaining
work are retained below; the linked runbook describes the newer component scope.

The newer [authenticated collector runbook](capability-kubernetes-collector.md)
documents bounded API collection, raw evidence custody and one-use activation
inspection. Its API-only command cannot substitute for missing independent
runtime witnesses and does not open the real-effects gate.

**This is not a validated local/Kubernetes activation backend. No real Job,
download, package install, connectivity probe or storage-exhaustion probe ran.**
The requested activation milestone is incomplete. `GatedRealCapabilityEffects`
remains blocked, control issues only synthetic packages, and provider-disabled
responses are unchanged. Do not treat a passing offline test as activation.

## Implemented independently of deployment

* `capability_activation.py`: strict/frozen versioned activation candidate schema;
  canonical JSON/digest and domain-separated HMAC verification; exact package,
  operation, tenant, project, worker, image and environment binding; bounded UTC
  freshness; a required 46-case probe inventory; and durable single-use candidate
  nonces. Failed evidence burns the valid manifest's nonce before verification.
  Reusing an operation, nonce, manifest or evidence digest in that journal fails.
* `capability_tls.py`: anonymous HTTPS GET primitive with a numeric pinned-IP
  socket, SNI, required certificate-chain/hostname verification, explicit CA-only
  trust, exact leaf DER hash, exact DNS SAN and issuer hash, peer-IP comparison,
  no redirects/proxies/decompression, bounded headers/chunks, total deadline and
  streaming expected-size/SHA-256 verification. It does not discover credentials,
  read proxy/CA environment variables, retry another address or publish artifacts.
  **It must not be composed as host-side broker effects.**
* `capability_watchdog.py`: persistent lease protocol core with authenticated
  sequential heartbeats, current-authority checks, expiry/revocation, durable
  kill intent, unknown state on lost replies, and authenticated historical
  termination evidence. Restart fences prior armed leases. The core has no
  submit/restart operation, daemon, HTTP listener or Kubernetes credentials.

The candidate verifier returns **only manifest/evidence digest identifiers**. It
does not validate the control package signature, grant an execution capability,
inspect Kubernetes or consume raw probe artifacts. It checks authenticated
observer-reported outcomes and their bindings; this is **not independent proof
that the probes ran or that their conclusions are correct**. HMAC custody and
observer independence cannot be established by the candidate's own JSON.

## Candidate manifest contract

`ActivationManifest.model_json_schema()` is the schema source. All fields are
required except `version=1`; all unknown fields, including nested fields, fail.
There is no CLI/environment enable switch and no key generation/provisioning.

| Group | Required identities and constraints |
| --- | --- |
| Activation | `enabled: true` (a boolean), `target: docker-desktop-kubernetes`, version 1 |
| Authority | `org_id`, `project_id`, `worker_id`, `operation_id`, `package_digest`, `environment_digest` |
| Target | `cluster_digest`, exact execution `namespace`/`namespace_uid`, distinct `control_namespace`, `service_account`/`service_account_uid`, `dispatcher_identity` |
| Image | immutable `image` with `@sha256:…`; exactly `command: ["/usr/local/bin/tessera-capability-sandbox-v1"]` |
| Objects | `namespace_spec_digest`, `rbac_spec_digest`, `admission_spec_digest`, `network_policy_spec_digest`, `resource_quota_spec_digest`, `pvc_spec_digest` |
| Output | dedicated `pvc`/`pvc_uid`, hard quota mechanism `filesystem-project-quota`, bounded output bytes/inodes, ephemeral bytes, memory and CPU |
| Network | one exact anonymous public HTTPS artifact URL, unique canonical public IPs, leaf certificate/issuer SHA-256, UTC DNS observation time |
| Watchdog | exact HTTPS endpoint, independent identity/certificate hash, 1–2s heartbeat, 2–5s lease, 0–2s kill grace |
| Lifetime | 1–120s timeout, UTC issue/expiry within 120s and within the package expiry, 256-bit run nonce |

The entrypoint is a **reserved contract name, not an implemented executable or
approved built image**. No image is pulled/built or silently chosen. The expected
namespace labels are derived by `manifest.labels`, including the tenant/project
scope digest and pinned restricted Pod Security version. A label declaration or
spec digest is not runtime readback or admission enforcement.

Document signatures cover `domain + NUL + canonical_JSON_bytes`, using HMAC-SHA256
with explicit keys of at least 32 bytes. Domains are `tessera-activation-v1`,
`tessera-probes-v1`, `tessera-heartbeat-v1`, and `tessera-termination-v1`. Approval,
observer and heartbeat keys must remain in separate deployment-owned custody;
never deliver them to a workload or accept agent-selected keys. This change does
not supply key lifecycle management or a remote signing/verification service.

## Evidence and activation runbook — stop at every unproven prerequisite

1. Retain the approved human policy and original signed operation package. Do not
   relabel a synthetic package as isolated. Implement a separately authenticated
   isolated-package issuance/check/claim/reconciliation path before live use; do
   not mount control state or signing keys in the workload.
2. Supply an explicit local target/CA/identity binding, approved node-present image
   digest and exact runner build. Do not use the ambient kube context, an exec
   credential plugin, cloud credentials or the shared control namespace.
3. Install/review a **new capability-specific** admission profile. The existing
   constant-pilot profile permits only `print(1)` / `print(2)` and cannot authorize
   this runner. Verify namespace/SA/PVC UIDs, labels, exact object specs, all
   relevant RBAC bindings, policy observed generations and warning-free CEL
   compilation. Check Job and controller-created Pod admission, including updates
   and ephemeral containers, with Fail/Deny rather than Ignore/Warn.
4. Independently execute every required positive and negative case in
   `REQUIRED_PROBES`. Retain bounded credential-free request/response transcripts
   and witness signatures. Include policy-attributed denials for hostPath,
   privilege, host networking/PID/IPC, extra containers, image/command changes,
   control-PVC mounts, Secrets and ambient tokens; effective RBAC forbidden-resource
   tests; and positive controls so outages cannot masquerade as denial.
5. Establish actual network enforcement. An allowed server must return the unique
   challenge; forbidden reachable targets need independently observed policy drops.
   Include public, private, metadata, control, alternate DNS and proxy paths.
   **A NetworkPolicy object, timeout, DNS failure or generic 403 is not proof.**
   TLS/IP/SAN/issuer mismatch and redirect tests must run against the actual
   transport/image, not just this suite's fake sockets.
6. Prove hard byte **and inode** enforcement on the exact output and ephemeral
   filesystems, including failed writes and archive expansion. Require actual
   quota errors at the approved limits and fresh isolation readback. ResourceQuota
   admission and ephemeral-storage requests/limits alone are **not filesystem hard
   quotas**. Kubelet eviction is not a synchronous hard runtime quota.
7. Deploy a genuinely separate watchdog and bounded authenticated control/Kubernetes
   adapters. Exercise broker loss, watchdog loss, revocation and expiry against a
   harmless fixture. The watchdog must be armed before work can start, bound to
   the exact Job UID, with no submit/arm gap. A background broker thread does not
   qualify. Authority callbacks and termination calls in the protocol core have
   no independent hard preemption; that enforcement is still missing.
8. Bind all readback and probe transcripts to the same manifest digest and fresh
   nonce. Current candidate validation accepts at most five-second-old probe/DNS
   observations. This tight bound needs a deployed collector; historical probe
   outcomes cannot just be re-signed with a new timestamp. Failed attestations
   require a new authorized investigation, never reuse of a failed candidate.
9. Only after a real independent inspector and enforcing runner exist, implement
   the bounded opt-in fixture runner outside default pytest/CI. Use a harmless
   hash-pinned fixture, no real model or package installation. Keep retries zero;
   retain exact operation, evidence, Job/Pod and termination identities. No such
   runnable live conformance harness is supplied in this partial implementation.

## Watchdog, rollback and reconciliation

The watchdog core registers a known suspended Job UID, checks independently
supplied authority, and persists an armed deadline. Each signed heartbeat must
increment the sequence by exactly one; duplicates/gaps fence the lease rather than
renew it. A delayed authority reply cannot extend an expired lease. Revocation,
clock rollback, missed heartbeats or restart request termination.

The core commits `unknown` before invoking the external termination callback.
Neither a callback return nor a deletion acknowledgement confirms termination.
After a lost reply there is no automatic retry. A separate authenticated observer
must provide fresh evidence for the exact Job UID, Job and owned-Pod absence and
credential revocation. Evidence predating kill intent cannot confirm it. The
current core validates the signed report/binding, not the underlying API responses;
the live observer/reconciler remains to be implemented and independently tested.

`terminated` is historical watchdog evidence only: it does not rewrite the broker's
unknown receipt, free a global claim, refund exposure or permit re-effect. Retain
the original broker journal and reconcile its existing hash-chained receipts and
quarantined bytes through the existing control sink. No new publication, release,
catalog or scientific-result acceptance path has been added.

Rollback means leave activation disabled, fence/revoke authority, request termination
through the independently verified path, and preserve evidence until actual stop is
confirmed. Never delete journals, nonce records, PVCs or uncertain Jobs merely to
obtain a clean retry. Do not relax admission or network policy to make probes pass.

The candidate and watchdog journals inherit private-directory, owner-lock,
no-symlink and synchronous-FULL SQLite custody from `CapabilityJournal`; their
tables are additive extensions, not changes to the control database. Their records
are not control-plane backups. Single-use claims are **local to that journal**;
global operation deduplication still belongs to the existing authenticated control
claim. There is no safe live deployment composition in this change.

## Verification and observed blockers

Offline tests use generated synthetic attestations, fake sockets and a simulated
external clock/termination adapter. No fake probe result is retained as live
activation evidence. No real evidence IDs were generated.

```sh
.venv/bin/pytest -q tests/test_capability_activation.py tests/test_capability_tls.py tests/test_capability_watchdog.py
```

Recorded task checks on the pre-existing dirty `main` working tree:

* The command above passed **187 tests**, with two existing Starlette/httpx/anyio
  deprecation warnings (28.11s).
* The same command extended with `tests/test_runtime_capability_broker.py`,
  `tests/test_runtime_capability_journal.py`, `tests/test_capabilities.py`,
  `tests/test_capability_fencing.py`, `tests/test_capability_contract.py`,
  `tests/test_pilot_isolation.py`, `tests/test_supervisor.py` and
  `tests/test_execution_contracts.py` passed **377 tests**, with the same two
  warnings (118.79s). Existing receipt-integrity, custody, replay and revocation
  tests were included; no new real-effect receipt path was validated.
* Scoped `ruff check` passed and `ruff format --check` reported **6 files already
  formatted** for the three new modules and their three test files. Initial
  import/format lint failures were repaired before these final checks.
* `git diff --check` passed; `git diff --no-index --check /dev/null PATH` also
  passed for each new file and the pre-existing untracked runtime-broker document.
* Full-repository pytest/lint/format integration gates were **not run**. These are
  targeted component/regression results, not completion of activation acceptance.

On 2026-09-22 the only live prerequisite observation was the read-only command
`docker --host unix:///var/run/docker.sock version --format '{{.Server.Version}}'`,
which returned **29.7.2**. `docker`, `kubectl`, the existing Python virtualenv and
Ruff executables were present. This is not proof Kubernetes is enabled/healthy or
that any required control is enforced. No kubeconfig/cloud credential was read.

**Missing deployment inputs:** explicit authenticated local target and CA binding;
approved capability-runner image/command; dedicated namespace/SA/PVC identities;
fresh independent network, quota, admission and broker-loss termination evidence;
external watchdog endpoint/identity and trusted observer custody. These were not
supplied or independently verified. Private historical pilot records were not
adopted as current authority.

**Remaining implementation work:** actual independent raw-evidence inspector;
Kubernetes admission/profile renderer for this runner; sandbox executable and
hook-free hash-locked installer with archive/no-follow protections; bounded Job
submit/output/kill adapters and remote authority channel; external watchdog daemon
and authenticated transport; isolated package issuance and broker receipt linkage;
global failed-attestation fencing; and a runnable bounded live conformance harness.
Until those are implemented and validated, real activation remains blocked.
