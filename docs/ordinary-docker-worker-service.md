# Ordinary Docker experiment-worker service seam

Intent revision 3; source-only continuation of the
[bounded adapter](ordinary-docker-execution.md). This is an experiment worker,
not a planner/critic runtime/profile, host command proxy or garage deployment.

**Current binding:** [ordinary-docker-http-binding.md](ordinary-docker-http-binding.md)
supersedes the historical missing-HTTP descriptions below. The ordinary CLI `run`
now constructs the actual schema-54 native backend and fixed Docker transport;
readback distinguishes execute/stop-only and exact persisted report delivery.
Its source tests use actual temporary ASGI HTTP/SQLite and synthetic Docker only.
The following original 31-test service checkpoint and prior binding limitations
remain historical evidence, not current CLI behavior or live acceptance.

| Check | User-facing journey | Owned components | Source proof | Remaining live boundary |
| --- | --- | --- | --- | --- |
| authority | Approved fixed worker/target claims an immutable experiment, renews within hard expiry, stops/reconciles the same container | `ordinary_docker_messages.py`, `ordinary_docker_service.py`, `ordinary_docker_worker.py` | Isolated strict-protocol + real private SQLite + synthetic Docker/authority tests | Actual native control HTTP binding and authenticated scope/epoch/lease predicates |
| integrated-journey | Ordinary independent-host experiment worker runs without desktop/harness keeper, captures Docker outputs, restarts and confirms cancellation | Service seam + `Dockerfile.guard` experiment artifact source | Source lifecycle, custody/ack/recovery and image recipe tests | Backend native Docker registration/outbox, approved built image, installed supervised worker and real garage run; laptop-off separately |

## Owned Python entry points and backend protocol

`OrdinaryDockerWorkerService(config, backend, adapter_factory=...)` exposes `step`,
continuous `run`, durable `request_stop`, and `close`. `max_cycles` is an isolated
test/embedding bound, not the normal service default. The service holds an exclusive
workspace flock and binds its journals to the fixed worker principal, HTTPS control
endpoint and immutable target digest. Reusing the workspace for another principal,
daemon/target or endpoint is refused. Runtime credential/CA paths are references only
in this source seam; they are neither opened nor logged by it.

`DockerWorkerBackend` is a closed injected authenticated-worker protocol:

| Method | Typed contract |
| --- | --- |
| `heartbeat(worker_id,key,timeout_seconds=...)` | aware authenticated heartbeat time; no execution authority implied |
| `claim(worker_id,target,key,timeout_seconds=...)` | `DockerClaim(dispatch_id,package,lease)` or acknowledged no-work `None` |
| `current_authority(claim,timeout_seconds=...)` | matching `DockerLease`; rechecks worker/credential/agent/grant/task/revision/epoch/tenant/destination/budget predicates, or denies |
| `renew(claim,key,timeout_seconds=...)` | same-epoch bounded lease receipt; never rewrites package/hard expiry |
| `report(claim,NativeDockerReport,key,timeout_seconds=...)` | exact `DockerDeliveryAck` |
| `output(claim,NativeDockerManifest,tuple[CollectedOutput],key,timeout_seconds=...)` | exact quarantine custody acknowledgement with actual control manifest ID |
| `cancel(claim,NativeDockerReport,key,timeout_seconds=...)` | stop/request/observed termination receipt, not inferred provider stop |
| `read(claim,timeout_seconds=...)` | `DockerControlReadback`: exact operation/container pins, desired run/cancel and bounded acknowledged delivery keys/digests |

These are **not guessed HTTP endpoints or compatibility promises**. The parallel
backend native Docker contract must be finalized before its authenticated HTTP client
is implemented. Adapter renewal's existing finite `/worker/leases/renew` call is mapped
internally to `backend.renew`; that mapping itself performs no HTTP request.

`NativeDockerReport` preserves actual provider `docker`, dispatch/operation/attempt/
lease/epoch/package/target pins, actual container ID (or unknown), observed pending/
running/succeeded/failed/unknown state, timestamp, termination confirmation and stop
intent. It never constructs a Job/Pod UID or claims a signal acknowledgement is an
exit. `NativeDockerManifest` retains actual container/operation/attempt/agent/worker/
grant pins and exact declared path/size/SHA256. It carries no prompt, raw logs or
scientific acceptance. The backend serializer must not manufacture an exit code:
the adapter observation does not expose a numerical failed exit code.

The inspected draft backend `DockerReport` currently names running/succeeded/
cancelled/unknown whereas this source report preserves pending/failed explicitly.
Finalize that state mapping and exit-code evidence without dropping unknowns or
fabricating success before HTTP binding. Final claim/read/ack/output/cancel endpoint
and receipt layouts are likewise parent/backend-owned integration work.

## Durability, authority and lifetime

- Claim/heartbeat keys are committed before calls. Unknown replies retain the same
  key; acknowledged no-work scans alone advance to a new scan key.
- A strict control-issued descriptor is committed before adapter effects. The
  service launch-intent bit is committed before submission. Restart of an uncertain
  operation is observation-only: missing adapter intent or missing container is
  unresolved, not permission to relaunch. A crash before adapter creation may need
  external reconciliation; it is not counted as zero cost or provider termination.
- Each step reads desired control state and refreshes current trusted authority.
  Ten-second lease renewal and thirty-second registry heartbeat use distinct durable
  keys. The approved package and immutable hard expiry never change. Denial, expiry,
  revocation, stop, malformed readback or backend error fences effects and attempts
  an exact-ID local stop of already-owned work without human/owner impersonation.
- Output bytes remain in the adapter's private SQLite custody before container
  capture-stop. Native output metadata and report/cancel payloads are journaled
  with fixed payload digests and stable keys before control delivery. Unknown
  delivery remains pending. Restart queries exact receipts or replays the identical
  payload/key; it never rekeys to hide an uncertain call or recollects a new workload.
- Every ACK must match operation/key/payload digest; output additionally requires
  actual manifest identity and quarantine custody. Completed queue ownership is
  released only after actual terminal observation and all pending deliveries ACK.
  A local stop can exit with recovery required and pending control delivery; stop
  intent does not erase those records or imply termination.
- `run(install_signal_handlers=True)` converts SIGTERM/SIGINT into local durable
  stop intent. It catches untrusted exception text rather than printing package
  argv/backend errors. The installing entrypoint must `close()` in its `finally`;
  the workspace must remain exclusive across ordinary supervisor restarts.
- The in-container guard still supplies immutable hard-deadline enforcement while
  the worker process is absent. Renewable lease/revocation fences require the trusted
  worker running/recovering. Installed failure/restart and enforcement witnesses are
  unverified; source success is not universal hard-stop or independent-host proof.

## CLI: explicit missing binding, never a fake functional default

```text
python -m tessera_worker.ordinary_docker_worker check [fixed reference flags]
python -m tessera_worker.ordinary_docker_worker run [fixed reference flags]
python -m tessera_worker.ordinary_docker_worker request-stop --workspace /owned/private/workspace
```

`check` and `run` require `--workspace`, `--worker-principal-id`, `--control-url`,
`--credential-file`, `--control-ca-file`, `--docker-endpoint`, `--docker-daemon-id`
and immutable `--image`. No arbitrary command, shell, plugin, bearer-value, SSH or
ambient Docker-context option exists. `check` reports **configuration shape only**;
it does not open auth files, discover service readiness, or contact Docker/control.
`run` currently exits **2** with `backend_http_binding_not_implemented` before any
auth read, filesystem state creation, network request or container execution.

`request-stop` writes only to an existing owned private worker journal and returns
`termination_confirmed:false`. It does not contact the daemon or clear a stop fence.
Actual observation/cancellation remains the running/recovering service's job.

## Guard artifact source, not an image build or target approval

`Dockerfile.guard` uses the repository's existing digest-pinned Python 3.11 slim
Bookworm base. It copies only the stdlib in-container guard into an isolated Python
package, sets UID/GID 65532 and packages no API/model/OAuth/Docker client. The typed
adapter supplies readonly root, network-none, no-new-privileges, cap drop, fixed
private tmpfs and resource bounds. No host volume/socket defaults or privileged
build/runtime behavior is added. The experiment's approved argv is part of its
immutable package, not a host command option.

No build, image pull, install or deployment ran. A separate authorized integrator
must build/inspect the Linux/amd64 artifact, record its actual resulting digest,
obtain human target/image approval and verify the installed guard/runtime limits.
The recipe/base digest alone is neither a tested artifact nor self-approval.

## Exact remaining integration

Parent/backend ownership: finalize source native Docker package/claim/current-authority/
renew/report/output/cancel/read and ACK layouts; bind the closed worker protocol to
actual authenticated HTTP, fixed endpoint TLS and worker-only credential references.
Retain errors as finite codes and exact unknown states. The service must not use an
owner token, source-local fake backend, Kubernetes outbox serializer, or callback
returning a cached lease as a functional fallback.

Then an admitted integration/deployment owner installs ordinary process supervision,
fixed daemon transport, exclusive workspace and approved built guard image, and runs
real off-host success, active worker restart, running cancellation, revocation/expiry,
native output readback/control provenance and notebook joins. Desktop/harness keeper,
planner/critic Compose/profile and laptop-off proof remain outside this source seam.

## Targeted source checkpoint (2026-10-05)

`pytest -q tests/test_ordinary_docker_service.py`: **31 passed in 4.20s**, no
warnings. Scoped Ruff lint/format passed for the three new worker modules and new
service test file. The earlier 89 adapter/guard/transport tests were preserved and
not redundantly rerun. Service tests use real private SQLite, the source adapter,
strict injected **synthetic** backend and synthetic Docker effects; CLI tests open
no credential files. Dockerfile checks are static source checks, not build/runtime
proof. No full suite, backend edit, planner/critic/profile edit, auth read, Docker,
SSH, garage, install, image pull/build, inference, deployment or commit occurred.

The six owned additions are the three service/message/CLI modules,
`tests/test_ordinary_docker_service.py`, `Dockerfile.guard`, and this document.
Existing Kubernetes, backend, runtime/profile and unrelated dirty changes remain
untouched. The worker is quiescent pending parent rebind with final native HTTP
contract; live integration is still explicitly unverified.
