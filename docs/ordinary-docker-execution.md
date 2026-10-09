# Bounded ordinary Docker execution seam

Source implementation for intent revision 3. This is **not** the local-networkless
conformance fixture, a host shell proxy, a deployed garage service, or proof that
the off-host journey works. Kubernetes paths remain untouched.

| Check | Journey | Owner/components | Required evidence | Prerequisites |
| --- | --- | --- | --- | --- |
| authority | Human-approved immutable package -> fixed worker endpoint -> bounded container -> renew/stop/recover | This worker: `ordinary_docker_contract`, `ordinary_docker_execution`, `ordinary_docker_guard`, `ordinary_docker_transport` | Isolated transport/clock/SQLite negative tests; live proof remains parent-owned | Authenticated control package + current-authority callback, installed pinned guard image, fixed Engine reference |
| integrated-journey | Same approved operation executes on garage, captures real Docker outputs, survives active-run worker restart, running container cancellation | Parent integration/deployment + control package/route registration | Real admitted garage Docker run with actual container ID, outputs, restart and confirmed stop; laptop-off separate | New supported control Docker profile/target and installed supervised worker; no OAuth copying or host shell delegation |

## Python APIs and immutable contract

- `DockerTarget(daemon_id, endpoint_digest, image)` fixes Linux/amd64 and guard
  version `ordinary-docker-guard-v1`; `.binding` is provider-neutral
  `TargetBinding("docker-container-v1", "ordinary-docker-v1", digest)`.
- `DockerPackage` is a strict, frozen, extra-forbidden model. It contains version,
  adapter/profile, operation/org/project/task/admitted-attempt/agent/worker/grant/
  lease/epoch identities, target and plan digests, aware hard expiry, digest-pinned
  image, immutable argv, CPU/memory/PID/scratch/output bounds, declared paths, and
  literal network `none`. `.envelope(target)` preserves the package digest and
  immutable hard expiry. No caller-supplied mount/env/socket/privilege field exists.
- `DockerLease` is the separately authenticated mutable lease receipt. IDs, epoch
  and hard expiry must match the package; current expiry must remain within hard
  expiry. It never rewrites the package or approval digest.
- `FixedDockerTransport(endpoint, tls=verified_context)` accepts a fixed worker
  Unix socket reference or hostname-verifying HTTPS endpoint, no ambient context,
  HTTP plaintext, SSH, CLI, authentication discovery or redirects. Runtime mTLS
  loading is the installing worker's job; workloads receive no credential reference.
  The finite API is `info/image/create/inspect/start/signal/archive`; no exec,
  attach, pull, build, global list, arbitrary archive path, or remove API is exposed.
- `OrdinaryDockerExecutionAdapter(private_journal, target, transport,
  current_authority=callback)` implements the provider-neutral `ExecutionAdapter`.
  The **trusted** callback must obtain/recheck current worker credential, grant,
  task/revision/epoch, tenant, budget and destination authority through the existing
  control plane and return the matching `DockerLease` or deny. A package constructor
  and Docker daemon discovery alone never establish human approval.
- `submit/observe/reconcile/cancel`, `collect`, `account`, `preflight/capabilities`
  use actual Docker container IDs; no Job/Pod UID synthesis. Unknown create/start
  replies keep durable one-shot intent; reconciliation never creates a replacement.
  A missing previously owned container is unknown, not proven termination.
- `renew(envelope, worker_channel)` journals stable key/body before
  `POST /worker/leases/renew`; `heartbeat` similarly calls `/worker/heartbeat`.
  Pending actions reuse the original key on restart. Fresh authenticated authority
  query may refresh a stale cached lease only for the same immutable identities.
  No expired lease is revived, no new key bypasses an unresolved command, and
  acknowledgement does not prove provider termination.
- `watch(envelope, worker_channel)` is the supervised continuous driver, not a
  desktop/harness keeper. It renews at ten seconds, heartbeats at thirty, observes
  the original container, captures declared output and stops on authority error.
  `max_cycles` is an isolated-test bound only. Installation must enforce exclusive
  worker workspace ownership and ordinary process supervision/restart.
  Restart with revoked/missing fresh authority still cancels the exact journal-owned
  container without heartbeat, renewal or replacement creation. `stop_status`
  exposes the durable requested-stop fence separately from observed termination;
  the control report must retain both rather than treating an exit as approval.
- `collect_outputs` returns real `CollectedOutput` bytes/digests only after a
  terminal successful container observation. A bounded tmpfs snapshot and its
  metadata are atomically retained in private SQLite before capture-stop; restart
  drains the same retained snapshot, not a replacement workload. `output_manifest`
  returns Docker/operation/attempt/package/target/lease/epoch/agent/worker/grant
  identity and each path/size/SHA256. This is **worker custody metadata**, not a
  control artifact ID, release, acceptance or scientific validation. Unknown cost
  accounting remains unknown, not zero.

## Container isolation and lifetime

The approved image must already be local by immutable repository digest and contain
the guard/module and experiment dependencies. No automatic registry pull is allowed.
Preflight checks pinned daemon/platform, declared memory/swap/CPU/PID enforcement and
seccomp support; implicit image volumes, healthchecks, on-build effects and secret/
proxy environment keys are refused. Live cgroup/mount enforcement still requires a
real runtime witness, not Engine capability flags alone.

Containers use UID/GID 65532, readonly root, no network/host PID namespace, capability
drop ALL, no-new-privileges, no restart/autoremove, no mounts/devices/socket, fixed
bounded private tmpfs `/scratch` and `/outputs`, byte/inode/PID/memory/swap/CPU limits
and no Docker logs. Direct argv launches without shell; workload stdout/stderr go to
DEVNULL. The in-image guard runs as private namespace PID 1, kills remaining namespace
descendants before output capture, and uses both an absolute hard deadline and a
monotonic backstop. It must never be invoked as a host helper.

Docker has no native Job deadline. The guard enforces the immutable **hard** deadline
even while the worker process is absent; current renewable lease and grant revocation
remain the trusted worker supervisor's fences. Loss of the worker itself is not
proof of immediate lease-expiry cancellation: installation/restart/stop must be tested.
No claim of equivalent Kubernetes hard-stop behavior during every failure is made.

Tmpfs disappears at container exit. After the direct workload completes, the guard
holds only until the worker captures outputs or the hard deadline. The fixed marker
is readiness metadata, not approval/termination proof. Archive reading rejects links,
special files, traversal, replay, PAX metadata, excessive entries/bytes and undeclared
paths. No extraction to host paths occurs. Paths are narrowed to bounded USTAR names.
Captured bytes remain quarantinable/untrusted research output; success is gated by
the daemon's later confirmed exit status. SIGTERM closes the completed guard after
capture; running-work cancellation uses exact-ID SIGKILL. A response alone is not stop.

## Parent-owned integration, not implemented by this worker

1. Backend package/profile admission must explicitly add `ordinary-docker-v1` /
   `docker-container-v1` with the strict field mapping above and existing human grant,
   tenant/budget/plan/target/image/attempt/agent fences. Do **not** reuse a local-cpu or
   Kubernetes package or let requests auto-enable this target.
2. Extend the closed control routing/profile models, worker capability enrollment,
   dispatch-outbox claim/report, and output-custody serializer for `provider=docker`
   and `container_id`. Existing `RemoteDispatcher` rejects non-offline preflight;
   simply passing this adapter there is insufficient and must not bypass that guard.
3. Add a small worker CLI/export wiring module (outside this worker's new-module
   scope) that loads only runtime references, builds this fixed transport/adapter,
   authenticates the current-authority callback, calls `watch`, submits immutable
   output bytes through existing control custody, and recovers the same journal.
   Existing Kubernetes dispatch descriptor/report code assumes Jobs/Pods and must
   not be silently repurposed. `runtime_packages` currently supports local CPU and
   Kubernetes only; package validation must be owned by the backend lane.
   Current execution HTTP routes do not expose GET-current-lease; backend ownership
   must provide a typed worker-scoped current-authority/lease query (or equivalent
   trusted in-process seam) using `_active_worker_grant` and immutable package pins.
   A cached descriptor is not a live authority check. Renewal responses alone
   cannot detect intervening revocation between cadence boundaries.
4. Package the guard into a separately approved Linux/amd64 image; operator identifies
   fixed Engine daemon/endpoint, validates enforcement, and supplies worker-only
   references behind ordinary supervised service configuration. Do not expose the
   daemon socket to planner/critic/experiment containers. No global cleanup.
5. An admitted deployment/integration owner—not this source worker—runs bounded live
   garage success, active restart, duplicate/ambiguous-start, revocation, hard expiry,
   running cancellation and exact output/control-notebook tests. Report source tests,
   live off-host execution and laptop-off evidence separately.

## Source acceptance receipt (2026-10-05)

`pytest -q tests/test_ordinary_docker_execution.py
tests/test_ordinary_docker_transport.py tests/test_ordinary_docker_guard.py`:
**89 passed in 4.93s**, no warnings. Scoped Ruff lint and format checks passed for
the four new worker modules and three new test files. Tests use synthetic Engine,
socket/HTTP, subprocess/signal and clock effects with real temporary SQLite/output
custody. No actual Docker daemon, workload, SSH, garage, service, credentials or
inference were used. Kubernetes and desktop gates were not repeated.

Coverage includes closed package/security/resource/output limits, target mismatch,
one-shot create/start and lost replies, exact container/attempt ownership, restart,
permanent stop/unknown acknowledgement, stable renewal and heartbeat intent replay,
expiry/hard limit/revocation/disconnect/clock and stop races, denied restart cleanup,
archive traversal/link/device/replay/size limits, declared digest capture and private
custody tampering, capture-stop uncertainty/recovery, and isolated guard hard timer.
The backend profile/capability/dispatch/manifest/current-authority query, installed
CLI/worker supervision, real image guard/runtime witness and live independent-host
journey remain explicitly unverified integration work—not permanent prohibitions.
