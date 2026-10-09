# 0021 — Independent sandbox supervision, not autonomy activation

Date: 2026-09-23. Status: local closed-fixture improvement selected; Kubernetes and
real autonomy activation rejected for this gate pending runtime evidence.

## Investigation and selection

Read the autonomy synthetic-package/receipt paths, local fixture/engine/probe,
Kubernetes runner/collector/activation and native witness/watchdog contracts. The
existing Kubernetes runner explicitly accepts only its fake client. Autonomy accepts
only issued synthetic packages and reconciled broker receipts. Neither boundary may
be bypassed by a sandbox vendor's status or a local fixture receipt.

Read-only local observations: Docker Desktop 29.7.2, Linux cgroup v2; explicit
`docker-desktop` Kubernetes context, node `desktop-control-plane` v1.36.1,
containerd 2.3.1. No CRDs or RuntimeClasses were listed. System DaemonSets were
kindnet and kube-proxy; local-path storage and existing Tessera deployments exist.
These listings establish availability, not CNI enforcement or deployment health.
No Secrets, raw kubeconfig, credential values, or workload logs were requested.
Repository search found no agent-sandbox/OpenSandbox/Kata/gVisor integration.

Public primary documentation inspected (moving main branches, not audited release
source or a deployment approval):

* [SIG Apps agent-sandbox README](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/README.md)
  and [threat model](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/docs/security/threat_model.md).
* [OpenSandbox README](https://github.com/alibaba/OpenSandbox/blob/main/README.md)
  and [operator](https://github.com/alibaba/OpenSandbox/blob/main/docs/architecture/control-plane/operator.md).

| Candidate | Isolation and controls | Lifecycle and termination | Availability/license and Tessera fit |
| --- | --- | --- | --- |
| Kubernetes SIG Apps agent-sandbox | Orchestrator, not isolation implementation. Delegates to RuntimeClass (gVisor/Kata). Pod templates supply non-root/read-only/resource controls. Managed template NetworkPolicy documents public Internet egress, not Tessera's exact allowlist/none. Bare Sandboxes require additional admission. PVC capacity is not byte/inode quota; PID enforcement still needs node evidence. | Singleton stateful Pod, stable identity, PVC, scheduled deletion, pause/resume and optional warm pools. Controller deletion is not a monotonic independent node kill or confirmation of no continued writes. Restart/adoption/resume must be fenced against no-retry authority. | Public Apache-2.0; README advertises v1.0.2. Not installed locally. Requires reviewed/pinned controller and runtime, exact UID/nonce binding, no pool reuse, independent kill/witness and signed receipt adapter. Router docs note default AllowAll authorizer: not acceptable as Tessera authentication. Reject deployment now; strongest future orchestration candidate, not a proven sandbox. |
| OpenSandbox Kubernetes / BatchSandbox | Docker/Kubernetes platform; optional gVisor/Kata/Firecracker isolation and egress services. Template limits and optional mounts need admission and runtime verification; filesystem/PID limits cannot be inferred from API. Broader command/file/credential interfaces enlarge trusted surface. | `expireTime` is controller-enforced absolute deletion, not proven independent monotonic kill. Pools, restart/recycle/Noop, task hooks, snapshots and resume conflict with closed no-retry/no-download scope unless disabled and inspected. `Succeed` means runtime ready, not task completion. | Public Apache-2.0, published images/SDKs, not present locally. No reviewed digest or release supplied. Needs authenticated lifecycle-to-operation mapping, raw node witnesses, immutable custody, global claim and zero retries. Reject deployment now. |
| Existing Tessera Kubernetes Job/collector | Strict non-root/read-only/admission/network/storage profile and signed challenge contracts, but native CNI/quota/CRI drivers and independent service deployment missing. No secure RuntimeClass observed. | Suspended Job arm-before-unsuspend seam; fake-only client. API absence cannot prove process death. | Already in tree, no install needed for offline tests; actual live prerequisites absent. Retain fail-closed seam, do not weaken inspector/defaulting or invent receipt. |
| Existing local Docker fixture + external guardian (selected) | Shared Docker Desktop kernel/daemon trusted, not hostile multi-tenant microVM isolation. Existing pinned local image, no network/mounts, non-root/read-only, cgroup/tmpfs probes and exact output custody retained. Namespace interface enumeration replaces sysfs directory listing, which is not a reliable network namespace witness. | Existing independently spawned supervisor kills workload; add a separately spawned guardian that owns that supervisor's child handle and recovers an interrupted exact container. Reciprocal authenticated liveness, own monotonic clocks, no workload restart. | Already available; no new software/images/dependencies. Docker Desktop licensing remains operator responsibility, not inferred from availability. Narrow harmless fixture only. Local hash-chain is not authenticated control/global authority. |

## Safety review and trust boundary (before any live attempt)

Select only the already-local image
`tessera-control-local@sha256:facbb6eda289f9e663a52f79abe1d0002ebab93bfb98046b00461206f140556c`:
Linux/amd64, image ID equals that digest, image user 65532:65532. Existing repository
Dockerfile builds this local control image; the fixture replaces its command with
a closed stdlib Python program. Presence/digest does not independently prove image
supply chain or absence of embedded secrets. Do not accept arbitrary agent images.

Trust host user/OS, Docker daemon/kernel and repository supervisor code. Workload
gets no Docker socket, control directory, broker key, guardian pipe or host mount.
The broker cannot renew absolute deadlines. Private inherited pipes use distinct
256-bit keys and exact package/nonce/sequences; HMAC establishes local channel
identity, not independence from a compromised same-user host. Evidence is retained
under the existing exclusive-lock, private, no-symlink SQLite custody boundary.

Guardian starts before the supervisor and any Docker effect, records arming, and
owns a real child process handle, never an agent-supplied PID. Single-threaded child
ownership prevents PID reuse while an unreaped child exists. It force-kills a hung
supervisor without depending on that supervisor's callbacks. After child exit and
exclusive journal acquisition it may make the **first** exact stop/cleanup attempt;
an already-recorded stop intent, including a lost reply, is not retried. A second
kill request after uncertain dispatch is not a harmless read. Interrupted execution
stays unknown even when termination/cleanup later become confirmed.

The supervisor detects guardian loss and stops the workload. Simultaneous loss of
both processes, host failure, unavailable daemon, uninterruptible kernel I/O or
corrupt/unavailable durable state remain unknown and need explicit reconciliation.
This is not a continuously installed OS service. No finite process hierarchy can
promise kill under loss of its trusted kernel. Cleanup authority is stop-only and
outlives workload authority; it cannot create/start/resume work or change nonce.

## Exact acceptance gates

1. Default command performs zero filesystem/process/Docker/API effects. Explicit
   run requires a fresh canonical evidence root and pinned already-local image;
   one closed wait workload, one create/start maximum, no retry/pull/build/install.
2. Digest/security readback and runtime probes must pass before release: UID/GID,
   namespace network interfaces (only loopback), dropped caps, no-new-privileges,
   seccomp, read-only root, CPU 250m, memory/swap 128MiB/0, PIDs 32, and actual
   ENOSPC byte/inode exhaustion on both exact 1MiB/32-inode tmpfs mounts. Missing or
   contradictory evidence blocks. Cgroup readback is not a claim that every
   CPU/OOM/PID pressure experiment was performed.
3. Workload ignores SIGTERM and waits; independent supervisor must send SIGKILL,
   inspect exact immutable container identity as exited (137), then remove only
   that non-running object and confirm absence with a successful complete list.
   A kill reply alone, missing inspect, agent exit, or missing Pod is insufficient.
4. Regression tests cover agent hang, broker/guardian/supervisor loss, monotonic
   expiry, ignored SIGTERM, stale/replayed authenticated messages, PID/container
   identity mismatch, lost create/start/kill replies, cancellation/unknown, exact
   cleanup after authority expiry, durable no-retry and no duplicate effects.
5. Only declared 28-byte output may enter unreleased fixture custody; the wait
   case produces none. Preserve existing archive/path/secret/custody regressions.
6. Exactly one authorized live invocation; stop on failed prerequisite, unknown
   evidence or cleanup, preserve its IDs. Do not retry with a fresh operation.
   No scientific/model/provider/Salad/download/install effects or cluster changes.
7. Passing local conformance still cannot issue isolated control packages, publish
   broker/autonomy receipts, satisfy Kubernetes witnesses or activate real effects.
   Future integration requires authenticated tenant/worker/session/package binding,
   live authority/revocation, global one-use claim, separately authenticated raw
   evidence, and existing transactional broker receipt/artifact reconciliation.

No third-party installation or Kubernetes deployment is selected or authorized by
this decision. Results and remaining gaps are recorded in the companion runbook.

### Namespace-proof amendment (2026-09-23)

The [follow-up](../local-docker-namespace-proof.md) narrows gate 2: neither sysfs
directory names nor `if_nameindex()` alone establish the namespace binding. Require
daemon-host procfs/sandbox-inode/process identity plus separately executed,
capability-free namespace-local rtnetlink and bounded connectivity controls.
Missing independent host observation blocks before create. The latest historical
failure really used `if_nameindex()`; it is not explained by merely replacing sysfs.
One follow-up preflight invocation was blocked on Darwin/LinuxKit with zero cases,
no retry and no live guardian/namespace success claim. Deploying a daemon-host
observation transport is a remaining implementation gate, not permission to add
privilege, mounts, packages or provider effects to the workload.
