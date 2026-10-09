# Transferable execution-plane monitor

The monitor is an **opt-in separate process boundary**, not a privileged control
service feature or an activated autonomy backend. See [decision 0022](decisions/0022-transferable-execution-monitor.md).
Default commands perform zero effects. No dependencies, services, cloud credentials,
images or providers are installed/selected automatically.

## Placement and trust

```text
control anywhere                         execution target
authenticated assignment issuer         guardian (owned-child deadline)
enrolled MonitorReceiver <== mTLS/HMAC ==> monitor (own clock + durable journal)
current authority/custody checks          exact-workload adapter
no host namespace/socket privileges       Docker fixture | K8s disabled | Salad disabled
                                                |
                                         isolated workload (no monitor keys)
```

* **Remote control + local execution:** deploy the monitor on the execution host,
  not beside the control database. Configure an explicit numeric control IP, TLS
  hostname/CA/server-certificate pin and a monitor-specific client certificate.
  `MonitorReceiver` needs no Docker/procfs access. Its caller supplies authenticated
  tenant/project/monitor enrollment and current worker/plan/package/operation
  authorization. There is deliberately no public self-enrollment endpoint.
* **Docker host monitor:** the implemented adapter attaches to the full container
  ID, creation identity, image, target daemon+boot digest and fixture nonce labels.
  It only accepts the fixed networkless `wait` fixture with zero declared output;
  it cannot create/start/release commands or arbitrary exec. Namespace observation
  runs at the **Linux daemon host**, not a macOS Docker client. Procfs reads, exact
  cgroup generation and sandbox namespace inode proof are mandatory. Docker's socket
  confers host-equivalent privileges: explicitly set `allow_host_socket=true` only
  on an isolated execution host under a reviewed service identity. Never put the
  socket, host mounts or signing keys in the workload/control container. No root,
  capabilities, host mounts or namespace entry are installed by the command.
* **Kubernetes sidecar / job monitor / DaemonSet:** contracts only, disabled. An
  assignment must pin cluster/node boot/namespace/Job/Pod/service-account UIDs, CRI
  container generation and native sensor identity. Kill/delete must use exact UID
  and resourceVersion preconditions. Use narrowly scoped per-operation RBAC; no
  wildcard exec, Secrets, cluster-admin or default privileged hostPID/hostPath.
  A sidecar sees only its allowed pod observations; independent node CNI/cgroup/
  filesystem sensors and an out-of-workload kill owner are prerequisites, not
  implied by pod adjacency. A job monitor must be armed before unsuspension. A
  DaemonSet design still needs a bounded sensor/capability boundary; no deployment
  YAML here grants universal host authority.
* **Future Salad worker monitor:** disabled identity contract includes group,
  allocation generation, worker/boot/image and native sensor certificate pins.
  Reallocation is not permission to adopt another worker or restart an operation.
  Provider cancellation, native no-continued-writes, cleanup and attributable
  accounting must be implemented independently; billing remains unknown.

Do not run guardian/monitor in the agent's process tree or workload cgroup. A
separate service supervisor may invoke stop-only recovery after monitor failure,
but must never relaunch a consumed assignment as new work. Simultaneous guardian,
monitor, host or daemon loss remains unknown; no software process can promise a
kill through an unavailable kernel/daemon.

## Assignment and evidence protocol

`tessera_monitor.protocol.Assignment` is frozen (including nested tuples/models).
It binds tenant, project, plan/package digest, operation, 256-bit nonce, distinct
monitor/worker/authority principals, runtime adapter, workload ID/image/generation/
target digests, named privilege profile, required evidence, limits and expiry.
The authenticated issuer must derive those fields from existing approved scope
and a globally claimed operation, not from a workload's claimed tenant. **Production
issuance integration is not enabled by this slice.** Fixture keys are not accepted
as real broker/control receipt authority.

`seal(assignment, authority_key, b"assignment")` produces canonical signed JSON.
Keys are distinct 32-byte runtime references, not inline JSON/environment/argv
values. Channels use separate HMAC domains for challenge/response, guardian peers,
evidence and acknowledgements. One outstanding random challenge binds assignment,
nonce, monitor, timestamp and consecutive sequence. Receiver sequence consumption
is durable before response; a lost response is not replayed to extend a lease.
The monitor checks its own wall and monotonic clocks before/after responses, with
an immutable absolute deadline and bounded lease. Revocation and loss are stop-only.

Remote `connect_tls` uses explicit IP, CA/hostname checking, TLS 1.3, client
certificate and exact server certificate hash; it never uses an ambient Docker,
Kubernetes, proxy or cloud configuration. A deployment-owned TLS listener must
authenticate/pin the client and select the already enrolled receiver **before**
calling `receive_one(channel, receiver, authorized=...)`. The receiver loop is a
library integration seam, not an installed server or new HTTP API route. Local
conformance uses a private inherited socket with HMAC instead of certificates.

Frames are length-prefixed and capped at 64 KiB with absolute read deadlines.
Assignments last at most 120 seconds; sequences/receipts and upload batches are
bounded. Receipt facts allow only named bool/integer/digest values: no environment,
tokens, certs, arbitrary commands, paths, raw stdout/stderr or upstream exceptions.
Observation origins distinguish daemon readback, host procfs, namespace probe and
journal evidence. Numeric/identity facts are the retained raw observations, not
unchecked operator narratives. Unknown is explicit for each unsupported category.

Evidence covers identity, namespace, network, resource/filesystem limits, custody,
watchdog, termination, cleanup and recovery. Namespace/network validation reuses
the existing independently bound Docker host/exec proof; safe network probe facts
and its digest are retained, not secret-bearing procfs diagnostic text. Native
cgroup memory/swap/PID/CPU and output `statvfs` values are observed where allowed.
Readbacks are **not** exhaustion tests. Missing kernel/filesystem enforcement
conformance or output capture keeps these categories unknown and fences the run.
An artifact link binds hash, byte count and custody receipt to the signed assignment;
receiver acceptance additionally requires an independent custody callback and
frozen-writer evidence. This Docker wait profile captures no artifact bytes.

## Durability, failure and reconciliation

Each operation has a private owner-only journal using the existing POSIX exclusive
lock and synchronous SQLite custody. Assignment and receipts are immutable;
receipts form a signed hash chain with consecutive sequence, wall time and monotonic
elapsed time. Preserve the entire directory and its runtime keys; never delete it
to retry. Use the same journal for the same assignment, including after crash.

The journal records stop/remove intent **before** dispatch. A kill request is not
proof of termination; exact non-running readback or a complete authenticated
inventory proving absence is required. Cleanup requires confirmed stop and exact
ownership, uses a full immutable ID (never a name/selector/prune/`rm -f`), and stays
available after authority expiry. Running-process PID/start-time changes, container
restart, replaced daemon boot or assignment mismatch fail closed. No arbitrary
host PID is ever signaled. Guardian signals only its owned unreaped child handle.

A journal restart is stop-only. If a stop/remove intent was already consumed, only
read reconciliation is automatic; no retry after a lost reply. Journal failure,
ambiguous identity or unavailable daemon remains unknown and requires investigation.
Historical upload/reconciliation cannot resume an operation or invent a successful
effect. Signed acknowledgements bind exact receipt digest and sequence; old exact
acknowledgements are no-ops, not cursor rewind. Receipts are retained after upload.

`MonitorReceiver.status()` always reports `activation=blocked` in this slice,
even for complete synthetic or authentic closed-fixture evidence. It validates
enrolled identity, assignment, signature, ordering, freshness and custody linkage,
but cannot manufacture live enforcement witnesses, worker/global claims or research
acceptance. Existing capability activation/watchdog/witness/collector, Kubernetes
runner, broker journal and autonomy provider-disabled gates remain unchanged.

## Commands and configuration

```sh
# No discovery, socket connection, state creation or process spawn:
.venv/bin/python -m tessera_monitor
.venv/bin/python scripts/check_execution_monitor.py

# Read-only prerequisite check (does not create a container):
.venv/bin/python scripts/check_execution_monitor.py --check-prerequisites

# Explicit service on the execution host, using owner-private deployment config:
.venv/bin/python -m tessera_monitor --run --config /PRIVATE/monitor.json
# Stop-only historical recovery; never renews/replays execution:
.venv/bin/python -m tessera_monitor --recover --config /PRIVATE/monitor.json
# Upload/reconcile only, including after expiry; no Docker call:
.venv/bin/python -m tessera_monitor --reconcile --config /PRIVATE/monitor.json
```

Start with [the disabled Docker config example](../deploy/monitor/docker.example.json).
Provision parent state directories, enrollment, signing keys and TLS material
explicitly outside the workload; runtime config/key/assignment files must be regular,
owner-private, non-symlink files. The command does not provision them. Assignment
workload IDs must come from the admitted worker's precreated **not-yet-started**
fixture; arm the monitor before start. The service does not close an external
dispatcher's create/arm gap or authorize starting arbitrary pre-existing work.

For a separately authorized single local case, **only after prerequisites pass**:

```sh
.venv/bin/python scripts/check_execution_monitor.py --run --allow-host-socket \
  --image 'ALREADY_LOCAL_REVIEWED_IMAGE@sha256:EXACT_DIGEST' \
  --evidence-dir /PRIVATE/NEW_EVIDENCE_DIRECTORY
```

The harness makes at most one create/start, retains intent before each, uses one
fixed networkless/no-mount/non-root wait program and never sends its `go` marker.
It arms a separate monitor/guardian before starting and retains local signed
receipts plus control reconciliation. `passed` means **monitor stop/cleanup only**,
not full isolation conformance, ignored-TERM kill proof, artifact capture or
activation. If creation/arming fails with an uncertain reply, retain exact intent
and investigate; the harness does not retry or delete by a broad selector.
No pull/build/install, provider calls, live Kubernetes or Salad effects occur.

## Validation and current local prerequisite

On 2026-09-23 the read-only prerequisite command returned exit **1**:

```json
{"activation":"blocked","effects":0,"gate":"daemon_host_namespace_observer_unavailable","host":"Darwin","state":"blocked"}
```

No live monitor conformance case was run: the macOS client lacks independent
Linux Docker-daemon host namespace visibility. This is a deployment prerequisite,
not permission to weaken the proof, install a privileged observer or relabel fake
evidence. No live container/evidence journal was created for this task.

Offline tests exercise signed assignment fields, immutable nested scope, deadlines,
control loss, replay/sequence/expiry, guardian termination of an actual owned hung
child, actual monitor crash/lock takeover, sockets/uploads, custody links, exact
Docker ID/nonce/PID reuse defenses, disabled portable adapters and fail-closed
activation. TLS configuration/pin tests are isolated doubles, not deployed mTLS
interoperability. Full integration/deployment conformance remains separate.

### Recorded task checks (2026-09-23)

```sh
.venv/bin/pytest -q \
  tests/test_execution_monitor.py tests/test_execution_monitor_docker.py \
  tests/test_execution_monitor_transport.py tests/test_execution_contracts.py \
  tests/test_salad_contract.py tests/test_capability_activation.py \
  tests/test_capability_watchdog.py tests/test_capability_collector.py \
  tests/test_capability_runtime_witnesses.py tests/test_capability_witness_live.py \
  tests/test_capability_kubernetes_runner.py tests/test_runtime_capability_broker.py \
  tests/test_runtime_capability_journal.py tests/test_autonomy.py \
  tests/test_local_docker_fixture.py tests/test_local_docker_guardian.py \
  tests/test_local_docker_limits.py tests/test_local_docker_namespace.py \
  tests/test_artifact_custody.py
# 1315 passed, 2 existing dependency deprecation warnings, 441.29s

.venv/bin/ruff check src/tessera_monitor src/tessera_control/monitor_evidence.py \
  tests/test_execution_monitor.py tests/test_execution_monitor_docker.py \
  tests/test_execution_monitor_transport.py scripts/check_execution_monitor.py
# All checks passed
.venv/bin/ruff format --check src/tessera_monitor src/tessera_control/monitor_evidence.py \
  tests/test_execution_monitor.py tests/test_execution_monitor_docker.py \
  tests/test_execution_monitor_transport.py scripts/check_execution_monitor.py
# 17 files already formatted
```

Both default CLIs returned `disabled`, `effects=0`, `activation=blocked`.
Tracked and new-file whitespace checks passed. These are task-scoped checks, not
the full repository lint/format/pytest integration gate. Existing unrelated dirty
work was preserved; no commit, push, install, cloud credential access, provider
call or live Docker/Kubernetes/Salad workload was performed. The only initial new
test failure was a historical upload acknowledgement that incorrectly rejected an
older exact receipt; it now safely leaves the durable cursor unchanged, and upload
replay/recovery tests pass.
