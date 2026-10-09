# Target-specific runtime witness adapters

## Evidence status and implementation boundary

**No live conformance has been run. This is not a supported Docker Desktop
enforcement profile.** Tests use serialized synthetic Kubernetes/native responses,
temporary cgroup-like files, and fake watchdog drivers/clocks. They establish
adapter/validator behavior, not deployed CNI, kernel or watchdog enforcement.

Implemented:

* `capability_witness_live.py`: Docker Desktop `NodeSensors` orchestration, strict
  API/native preflight, fixed bounded network/quota/watchdog operations, actual
  API admission dry-runs and disposable positive creates, and exact cleanup.
* `capability_witness_native.py`: one-use authenticated native RPC and raw CRI,
  cgroup file, filesystem quota, network-flow/socket/HTTP/DNS and watchdog decoding.
  The collector revalidates raw/normalized agreement, not just a success flag.
* `capability_witness_supervisor.py`: separately hosted native RPC custody,
  Linux directory-FD cgroup reads, one-byte/inode quota sentinel, and independent
  monotonic lease/kill/quiet-window watchdog primitive. These do not discover
  container sockets, paths, credentials, quota drivers or executable commands.
* `capability_witness_target.py`: strict target and daemon-composition schemas,
  explicit mTLS certificate references and DER fingerprint pins.

**Remaining implementation/deployment gaps:** a complete privileged native
`NativeDriver` for a particular Docker Desktop node is not supplied. In particular,
mapping CRI containers to cgroup/mount FDs, creating and supervising the approved
network/pressure fixtures, obtaining CNI per-flow verdicts and safe routing-class
equivalence, and obtaining filesystem `quotactl`/ioctl project-quota snapshots
require reviewed platform drivers. `native-policy-flow-v1` and
`linux-project-quota-v1` name strict wire contracts, not installed Cilium/Calico/CSI
drivers. The RPC service core does not implement these facilities by signing an
operator's assertion. A clean checkout cannot run all four live witnesses.

No fixture image, native binary, privileged DaemonSet, ServiceAccount, admission
policy, certificate, credential, quota filesystem or CNI is installed/generated.
No cloud, paid provider, model or data download is used. The reviewed fixture image
must already be present on the exact node and use `imagePullPolicy: Never`.

## What Docker Desktop can and cannot prove

| Observation | What it establishes | What still blocks |
| --- | --- | --- |
| Explicit API CA, kube-system UID, node UID/boot ID, Kubernetes version and SelfSubjectReview | Authenticated API/target identity at preflight | Not CNI or kernel enforcement |
| NetworkPolicy object or denied connection timeout | Requested policy or connection ambiguity | No pass without the exact policy's per-flow DROP and a fresh healthy fixture endpoint |
| Working HTTP proxy or inherited proxy variables | A possible bypass | Always rejected; direct socket/HTTP/DNS observations are mandatory |
| CPU/memory requests and PVC/ResourceQuota/emptyDir sizes | Requested admission/storage limits | Not observed `cpu.max`, `memory.max`, PID/OOM events, filesystem hard byte/inode quotas |
| Stock local-path provisioned volume | A provisioned path/capacity | Cannot establish a project hard quota or dedicated enforcement merely from capacity |
| Independent Linux cgroup v2 files plus matching CRI container identity | Observed limits and counters for that exact fixture | Missing cgroup files, wrong node/image/identity, absent throttling/PID/OOM evidence remain unknown |
| Filesystem EDQUOT sentinel and native hard-quota counters | One measured hard-boundary attempt for the exact dedicated mount/project | ENOSPC, application counters, partial writes, changed bytes/inodes, underreported use or shared storage do not pass |
| API delete/Pod absence or a dead broker | API state or loss of broker | Not confirmed process termination or absence of continued writes |
| External clock/heartbeat, single kill/revoke, exit 137, empty cgroup, stable write count for a full heartbeat interval | Bounded independent termination evidence | Lost heartbeat/kill/observer evidence is unknown; no retry |
| Native API dry-run plus actual positive readback | Actual admission/defaulting behavior | Any unreviewed default/mutation or ambiguous denial blocks |

No Kubernetes native default-removal profile is approved here. The sole admission
profile, `exact-no-unreviewed-defaults-v1`, compares all workload/security/image/
command/resource/mount fields. Ordinary Kubernetes Job selectors/template labels,
scheduler defaults or admission defaults may therefore block. Those fields must
not be silently stripped. Dangerous controls are **dry-run only**; actual Jobs
remain suspended and actual Pods must be the separately reviewed harmless fixture.

The existing admission policy may also reject CNI fixtures' distinct ServiceAccounts
or endpoint workloads. That incompatibility is unknown/block, **not** permission
to weaken the policy or bypass admission. A future reviewed fixture/defaulting
contract must establish equivalent enforcement without widening execution authority.

## Explicit conformance CLI

The existing script now accepts target-specific options:

```sh
# Zero effects: does not even read --target, credentials or journals.
.venv/bin/python scripts/check_capability_runtime_witnesses.py \
  --target /absolute/private/target.json

# Offline schema only; no target/credential reads or network.
.venv/bin/python scripts/check_capability_runtime_witnesses.py --target-schema

# ONLY after separately deploying/reviewing native drivers and opting into effects:
.venv/bin/python scripts/check_capability_runtime_witnesses.py \
  --live --target /absolute/private/target.json
```

`--live` requires `--target`; `--config` is not an alias in this mode. Unknown schema
fields are rejected. `TargetConformanceConfig` extends the existing authenticated
collector config with:

* Exact `TargetProfile`: plan/cluster digests, Docker Desktop context, node name,
  UID/boot ID, API/kubelet version, image digest, reviewed fixture-binary digest,
  native driver protocols, budgets and zero retries.
* Eight exact source and eight exact endpoint ServiceAccount **name/UID pairs**,
  indexed by `allowed`, `allowed-dns`, `public`, `private`, `metadata`, `control`,
  `dns-bypass`, `proxy`. They must be distinct, non-default and credential-free.
  Namespace UIDs and the main workload ServiceAccount UID come from the manifest
  and witness pins. The adapter never creates or adopts these accounts.
* All four witness pins with the same `target_digest` (SHA-256 of canonical profile
  JSON). Each witness has a distinct identity and server certificate. Watchdog
  endpoint, identity and certificate must equal the manifest's pins.
* `witness_tls` for each channel: absolute certificate/private-key file references,
  local certificate DER SHA-256, and peer certificate DER SHA-256. Existing explicit
  CA/bearer and separate request/report HMAC references remain mandatory. Nothing
  is read from ambient kubeconfig, current-context, proxy, cloud SDK or CA stores.

These pins are authorization constraints, not claims that enforcement exists.
The CLI never accepts raw observation/report files, native commands or plugins.
Legacy `--witness-live` remains a protocol diagnostic. Legacy reports with no
target digest can no longer authorize a non-fake `CollectorActivationGate`.
Default isolated fake tests retain their explicitly labeled test-only path.

## Independent service composition

Deployment code constructs `LiveWitnessTarget`, then `compose_live_program` and
`WitnessDaemon` with the exact approved plan and deployment-owned harmless-image
allowlist. Host it with `serve_witness` outside both the broker and workload/control
namespaces, using a server TLS context with `CERT_REQUIRED` and an explicit
`client_certificate_sha256`. The client verifies CA, IP SAN and exact server DER
fingerprint before sending credentials. Both sides additionally authenticate HMAC
payloads and bearer channels. No server is started by import or by the default CLI.

Each native service uses `NativeSupervisorService` with an authenticated current
challenge, the same target profile, separate keys/journal and a reviewed
`NativeDriver`. How the independent services receive that approved fresh challenge
and provision their Linux driver is deployment composition, not an upload/report
RPC. The native service's fixed operation vocabulary has no shell or arbitrary
URL endpoint. A driver must enforce its own exact fixture handle ownership,
create-intent journaling, Kubernetes UID/version cleanup, no credential mounts,
image presence and hard process deadlines. Interfaces alone do not prove that it
does so. The new primitives do not constitute a full native driver implementation.

The external watchdog primitive uses its own monotonic time, survives broker
suspension, burns kill intent before dispatch, and refuses a second kill/replay.
Restart loses heartbeat evidence rather than inventing a successful continuation.
Node process/cgroup and write-count inspection remains independent of the fixture.
The service manager must terminate blocked native syscalls; cooperative Python
deadline checks cannot preempt a hung kernel or malicious driver.

## Bounds, custody and cleanup

The original five-second freshness deadline is unchanged. Slow scheduling, policy
convergence or lease/quiet intervals that do not fit **block**, without extending
freshness. Per target witness: at most 128 combined API/native calls, 2 MiB response
budget, 32 created resources, one packet per CNI category, zero retries. Quota
pressure retains the existing memory + 1 MiB, PID + one, CPU 250 ms and bounded
output/ephemeral limits. The Linux sentinel itself attempts only one extra byte or
inode at an already independently measured, dedicated hard boundary. It never
fills a host/shared disk to discover capacity. Collector API bounds remain separate.

Adapter and native journals retain immutable attempts and replies. Requests are
consumed before effects; unknown/lost replies do not free the nonce. Known transport
credentials and suspicious secret fields are rejected before response custody.
Error text is fixed-category, never raw provider output. This does not detect all
possible covert secret encodings; fixture-only data and private custody are required.

Actual admission cleanup uses only successful exact create handles, fresh namespace
UID and full nonce checks, UID/resourceVersion delete preconditions and one NotFound
readback. Native cleanup operates from the native service's own durable successful
creates, not a client-supplied inventory. No selector/prefix/namespace deletion,
resource adoption, retry or automatic PVC deletion is allowed. Uncertain cleanup
blocks activation and requires separately authorized reconciliation.

Conformance is operational evidence only. It does not issue a grant, start a real
runner, change broker authority or publish a research receipt. A missing, stale,
unknown, contradictory or lost required witness blocks the entire activation.
