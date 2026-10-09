# Local Docker namespace proof — fail closed

## What the historical evidence actually measures

On 2026-09-23, historical source/package bindings and all receipt chains were
independently rechecked without changing old SQLite or JSON evidence:

| Evidence | Bound network predicate | Result |
| --- | --- | --- |
| `tessera-local-networkless-20260923-d` | `os.listdir('/sys/class/net')` | Undifferentiated limits unknown, exit 78; 12 events |
| `tessera-local-networkless-20260923-e` | `os.listdir('/sys/class/net')` | Network assertion, exit 78; 12 events |
| `tessera-sandbox-supervision-20260923-one` | `socket.if_nameindex()` | Network assertion, exit 78; 14 events |

`/sys/class/net` enumerates the view associated with the **sysfs mount**. It does
not independently identify the calling process's current network namespace. A
host/shared sysfs mount can therefore produce extra names even in a private
network namespace; conversely a loopback-only sysfs view cannot prove isolation.
`socket.if_nameindex()` queries the calling process's namespace, not that sysfs
directory. The latest retained package binds the latter program (program SHA-256
`5a98889238b0c22ba5abe2fca40b1f3ca3597a927a7c93842cdb14dc1bcaed40`).

**It is incorrect to explain the latest failure as merely the old sysfs check.**
None of these records retained namespace inodes, interface names, routes, or
addresses. They cannot establish whether historical sysfs was host-shared or why
the latest namespace query disagreed with `NetworkMode=none`. Docker declarations
and a failed assertion do not establish external connectivity. The original d
record does not even identify which assertion failed. No historical result is
upgraded to isolation success.

Observed runtime: macOS Darwin 24.6.0 x86_64 supervisor; Docker Desktop 4.87.0,
Engine 29.7.2, LinuxKit 7.0.12, amd64, cgroup v2, default runc (1.3.6), containerd
2.2.5, built-in seccomp. The macOS process cannot directly inspect LinuxKit's
`/proc/<Docker State.Pid>/ns/*`. A host PID reported by Docker is a **VM PID**, not
a macOS PID. Missing independent access must not be replaced by a guessed inode.

## Implemented proof

`local_docker_namespace.py` supplies two mandatory observation paths:

1. A trusted **daemon-host** reader checks procfs network/mount/PID namespace
   links, inode/device identities, process start ticks, exact command digest,
   container cgroup binding, Docker's `SandboxKey` inode and inspected immutable
   container ID/start time. Host namespaces and known running Docker peers sharing
   the workload network namespace are rejected. Before/after snapshots must agree.
   It must run in the daemon host's namespaces with permission to read this
   evidence. It does not enter a namespace, create a helper container, mount host
   paths, or grant workload capabilities.
2. A fixed stdlib-only Python observer is sent by the supervisor through exact-ID
   `docker exec`, under the existing non-root, read-only, cap-dropped profile.
   `/proc/1/ns/*`, observer namespace identities and PID-1 start/command identity
   must agree with the independent reader. Kernel rtnetlink dumps enumerate the
   actual observer/workload interfaces, IPv4/IPv6 addresses and routes. Dumps are
   size/time bounded; kernel sender, sequence, completeness, malformed messages
   and interrupted dumps are checked. No `ip`, shell, curl, package installation,
   privileged mode or additional image is required.

The proof accepts exactly loopback, 127.0.0.1/8 and ::1/128, and only reviewed
loopback local/broadcast routes. Missing IPv6 observation is unknown, not a waiver.
It retains sysfs names/mount information for comparison; any disagreement remains
`ambiguous_sysfs_visibility`, not an assertion of leakage and not a pass.

Bounded connectivity controls use owned ephemeral loopback sockets in each address
family: a positive TCP connection transfers the operation nonce; a bound,
non-listening socket must refuse connection. Negative non-loopback probes use only
documentation literals `192.0.2.1:9` and `[2001:db8::1]:9`, after the observer sees
loopback-only addresses/routes. They must return Linux `ENETUNREACH`; timeout,
refusal, unexpected success or unavailable observation blocks. Each socket timeout
is 0.2 seconds, with no retries. There is no DNS, HTTP, metadata endpoint or public
service. Raw sockets here mean unproxied TCP API calls (not privileged raw IP
sockets); route inspection uses unprivileged AF_NETLINK. Proxy environment keys
are rejected, not silently bypassed.

The observer source digest now participates in each **new** fixture package digest.
Old journals still require their original source; do not open them under changed
code to rewrite or retry their operations. A workload `ready` transcript is only
one prerequisite: independent network proof and the original deadline/lease must
also pass before `/scratch/go`. An unavailable observer blocks before create.
Failures after create still follow exact-owned stop/readback/non-force removal.
Guardian, non-root, no host mounts, no network, dropped capabilities, read-only
root, tmpfs exhaustion, resource bounds, quarantine and no-retry rules remain.

## One authorized invocation, no conformance case launched

The one-case harness was invoked once after targeted tests passed. It stopped in
read-only preflight; **no second invocation or container attempt was made**:

* Local evidence ID: `SANDBOX-NAMESPACE-ONE-ATTEMPT-2026-09-23`
* Files: `preflight.json`, `supervision-summary.json`, `verification.json`
* Gate: **`daemon_host_namespace_observer_unavailable`**
* State/activation: **blocked**; attempts: **0**; automatic retry: **false**
* Operation: `33dfe4ab501a676caa5e5208d87b21d875a1dbb9f312b92cb354735dde0ac0f4`
* Nonce: `25213bdcd6a183d4a03577b70a2746f93309502df6de008768a35fa968b20589`
* Package: `6b3531ebe6c15b5ec8a8f483bc62a818df44c06ec54a5e2af719b2607508d74b`
* Already-local image ID: `sha256:facbb6eda289f9e663a52f79abe1d0002ebab93bfb98046b00461206f140556c`
* New container ID: **none**. No `case/` journal or run intent exists.

Fresh successful complete Docker listings confirmed all three historical exact
container IDs absent, and no containers with the local fixture label. Historical
d/e security readbacks passed the security verifier; the latest supervision
summary does not retain full inspect/security observations. All three original
journals have zero output artifacts and no workload release/custody. No new
control receipt, artifact, guardian execution or termination proof was generated.
Private evidence remains outside Git; nothing unrelated was removed.

Verification: **218 passed in 31.51s**, using:

```sh
.venv/bin/pytest -q tests/test_local_docker_namespace.py \
  tests/test_local_docker_fixture.py tests/test_local_docker_limits.py \
  tests/test_local_docker_guardian.py
```

These include the actual observer parser/probe functions against synthetic
kernel/socket replies, host-sysfs false positives, shared namespaces, wrong inode/
PID identity, extra interfaces, route/address/connectivity changes, missing state,
proxy rejection, preflight-before-create and release blocking. The first invocation
had one test setup failure (missing temporary parent directory); repaired before
the passing run. Existing guardian process, exact cleanup and artifact rejection
tests remain passing. No default test contacts Docker/network/providers.

## Remaining prerequisite, not a permission workaround

To obtain live proof, an independently trusted observer must be available on the
LinuxKit **daemon host**, or an equivalently authenticated native observation
transport must be implemented and deployed. No such transport is currently
configured. Do not use a privileged helper, host bind mount, workload self-report
alone, weaker predicate, or newly installed workload tools to evade this gate.
The new native reader/exec path is offline-tested, **not live-validated** here.
Actual namespace cause, downstream limits, independent watchdog termination and
artifact behavior remain live acceptance gaps. Even a future passing fixture
cannot activate autonomy, downloads, installs, providers or Kubernetes.
