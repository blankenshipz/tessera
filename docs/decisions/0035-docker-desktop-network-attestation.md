# 0035 — Named network attestation classes for the local Docker fixture

## Status

Accepted. (ADR 0034, governed sandbox egress, is merged on main.)
Option (B) is implemented and amends the "Remaining prerequisite" paragraph of
[local-docker-namespace-proof.md](../local-docker-namespace-proof.md): the strong
gate and its default are unchanged, and the weaker class is separate and explicit.

### Human decisions (approved)

1. Add the explicit, operator-selected `engine_declared_workload_observed` class,
   recorded on receipts and journals and never relabelled as
   `daemon_host_independent`. The strong predicate and the default behavior stay
   unchanged.
2. This is a **narrow exception to the ADR 0033 freeze**, because it is required to
   validate the platform (live supervision paths on Docker Desktop). It does not
   lift the freeze for ordinary-docker, the witness planes or any other surface.
3. A weak-class result may close platform-validation items 1a and 5 only as
   **"PASS (weak attestation)"**, reported separately and never folded into plain
   PASS. Network-isolation claims stay **GAP** under the strong class.
4. Bounded live local Docker runs through Tessera are authorized separately from
   this implementation, by the human, later. Implementation ran no Docker.

Remaining open items from the original draft (strong-class paused re-check,
Linux-host operator choice) are resolved as drafted: the re-check is weak-class
only (strong behavior unchanged), and the weak class is operator-selectable on any
host but is never an implicit fallback.

## Context

[Platform validation](../platform-validation.md) GAP 1a (network-none sandbox)
and GAP 5 (stop/cancel/deadline/cleanup) are open on macOS Docker Desktop
because `NativeNamespaceIdentity.preflight` stops with
`daemon_host_namespace_observer_unavailable`: the macOS supervisor cannot read
LinuxKit `/proc/<pid>/ns/*` or `/var/run/docker/netns`. No container is
created, so the cancel, deadline, broker-loss and cleanup paths of
`supervise`/`terminate` never run live.

The current proof combines two observation paths:

1. **Independent daemon-host reader**: namespace links/inodes of the workload
   PID versus host PID 1, `SandboxKey` inode binding, cgroup and command binding
   to the immutable container ID, no Docker peer sharing the netns, and
   before/after host snapshots that agree.
2. **In-workload observer** (`OBSERVER`, via exact-ID `docker exec`): rtnetlink
   link/address/route dumps, uid/capability/no-new-privs/seccomp status, proxy
   environment, sysfs comparison and bounded loopback/documentation-literal
   connectivity controls.

Path 2 already runs under the image's own `/usr/local/bin/python`, so even the
strong proof trusts the pinned image's interpreter and the kernel for
enumeration. What path 1 adds is **independent binding**: that the namespace
enumerated is the workload's, that it is not the host's or a peer's, and that
it did not change. On Docker Desktop path 1 is unavailable.

Termination and cleanup evidence is engine-observed in every class: exact-ID
`inspect` showing not-running, a non-force `rm ID`, and a complete
`container ls --all` without the ID. The namespace reader never contributes to
it. GAP 5 is open only because release-gating blocks before create.

## Threat model

The positions considered are: misconfiguration or engine regression
(`NetworkMode=none` declared, but non-loopback links, routes or reachability
present); a malicious or trojaned image (the interpreter or libc forges
observer output; the image is digest-pinned and reviewed, not verified); engine
or VM compromise (an exec into the wrong namespace, false inspect data, or a
netns shared by a non-Docker VM process); a change after observation (`docker
network connect`, a late-joining peer); and evidence relabelling (a weaker
record presented, consumed or upgraded as strong). Trusted in every class: the
supervisor host, the Docker CLI transport to the local socket, the LinuxKit
kernel, and the pinned image's bytes.

## Options

**(A) In-VM observer container** (`--pid=host`, perhaps `--net=host`, plus a
read-only mount of `/var/run/docker/netns`). Rejected for now. It uses exactly
what the contract forbids: a helper container, host namespaces and a host bind
mount. Reading another process's `/proc/<pid>/ns` needs ptrace-read access
(the same uid, or `CAP_SYS_PTRACE`), and a host-PID helper sees every VM
process, including the daemon's command lines. It is also **not independent
of the engine**: the daemon whose declarations are in question places it in
"host" namespaces.

Adopting (A) would need an ADR amending namespace-proof and 0021, a separately
pinned helper image, exact-ID ownership, guardian and cleanup for the helper,
a threat analysis of host-PID exposure, and its own class name (for example
`engine_placed_vm_observer`). It must never reuse `daemon_host_independent`.

**(B) A named, weaker attestation class** (recommended):
`engine_declared_workload_observed`. It uses engine declarations plus the
unchanged in-workload kernel observation, with no independent daemon-host
reader. The operator selects it explicitly and every receipt records it. It
can never satisfy or be relabelled as `daemon_host_independent`.

**(C) Status quo.** GAP 1a and 5 stay open on macOS, and the strong class is
proved only on a native Linux Docker host, where the existing reader runs.
This is honest, but the supervision paths get no live evidence on the
development host. (C) remains the only route to the strong class; (B) does not
replace it.

## Decision

Adopt (B). Keep (C) as the only route to `daemon_host_independent`. Defer (A).

### Classes

| `attestation_class` | `observer_independence` | Proof `state` |
|---|---|---|
| `daemon_host_independent` (default) | `independent_daemon_host_reader` | `verified` |
| `engine_declared_workload_observed` | `none_engine_and_image_trusted` | `workload_observed` |

The weak class never emits `state="verified"`. A consumer must match
`attestation_class` by exact equality; truthiness or `state` alone is not
enough.

### What the weak class proves, and what it does not

At one observation point before release, assuming the engine, kernel and
image interpreter are trustworthy, it proves:

- **Engine readback**: before and after observation, for the exact immutable
  ID, the engine reported `NetworkMode=none`, `Networks=={"none"}` and the full
  `verify_security` profile.
- **Peers**: no listed Docker peer declares `container:<id>` network, PID or
  IPC mode.
- **In-workload view**: a process exec'd by the engine into the exact
  container saw only `lo` with `127.0.0.1/8` and `::1/128`, only the reviewed
  loopback routes, and no proxy environment.
- **Observer identity**: uid 65532, `CapEff` 0, `NoNewPrivs` 1 and seccomp
  filter mode. Its namespaces equal `/proc/1`'s, and the `/proc/1` command
  digest equals the engine-declared entrypoint and command.
- **Connectivity controls**: an owned-loopback transfer succeeds, a bound port
  refuses, and `192.0.2.1:9` and `[2001:db8::1]:9` return `ENETUNREACH`.

It catches real misconfiguration: a container placed in the VM's netns or a
populated peer netns shows extra links or routes and blocks.

It does **not** prove that the enumerated namespace is the workload's: a lying
interpreter or an engine exec into another namespace goes undetected. Nor does
it prove that the namespace is not the VM host's (it relies on content, not
identity), that no non-Docker VM process shares the netns, that `SandboxKey`,
cgroup or command bind to a host process, that isolation holds after the
observation point, or anything about connectivity beyond the two literals. A
compromised image or engine can forge a passing record. This is a
configuration attestation, not an adversarial isolation proof.

### Fail-closed rules

1. **Explicit selection, no fallback.** The default is
   `daemon_host_independent`, and the weak class needs an exact-value operator
   selection. A strong-preflight failure stays `blocked` with its existing
   gate and never retries as weak, and nothing falls back the other way.
2. **Binding before effects.** The class is bound into the package digest
   before claim, and the config class must equal the plan class. A mismatch,
   or an unknown or missing class, raises `BackendBlocked` before any
   filesystem, process or engine effect.
3. **Same in-workload predicates.** Every in-workload predicate is exactly the
   strong one; only the independent-binding checks are absent. Any
   workload-side fault blocks both classes with the same gate.
4. **Class recorded everywhere.** `network_preflight`, `network` and the
   report carry `attestation_class` and `observer_independence`. `report()`
   raises `JournalConflict` if a recorded value differs from the plan-derived
   class.
5. **Weak-class re-check.** The weak class repeats the engine-declared
   security readback on the paused inspect before custody. A mismatch makes the
   outcome blocked or unknown.
6. **No activation.** `activation="blocked"` is unchanged. Neither class
   activates capabilities, publishes control receipts, releases artifacts,
   satisfies Kubernetes or agent-sandbox gates, or supports scientific claims.

### Stop, cancel, deadline and cleanup under the weak class

`supervise`, `terminate`, `recover` and the guardian (`preclaimed`) path are
unchanged and class-agnostic; the class changes only which observer gates
`/scratch/go`. These stay identical: arm before create; at most one create and
one start; durable unknown before kill; `kill_pending` and stop claims;
exact-ID inspect before every destructive call; label, name and image
ownership; non-force `rm ID`; complete-list absence; no retry; and guardian
fencing of forward work only.

Each of the five CLI cases uses a fresh operation and nonce. Their stop and
cleanup records are engine-observed in both classes and labelled with the
run's class, so a weak-class run gives live GAP 5 evidence without implying a
strong network proof.

### Downgrade and upgrade

A weak record never counts as strong evidence. Neither combining weak records
nor a later strong run (a new operation with new identity) upgrades it.
Historical journals keep their bytes and digests: a record with no class is
read under its original profile (`local-networkless-fixture-v1`, strong) and
none was ever `verified`. There is no backfill, and moving, editing or
re-digesting a weak journal into the strong class must fail integrity checks.

### Config surface

`FixturePlan.profile` adds `local-networkless-fixture-engine-attested-v1`. The
class is *derived* from the profile, so there is no second field to desync,
and existing v1 plans keep a byte-identical `model_dump` and digest.
`LocalFixtureConfig.attestation_class` defaults to `daemon_host_independent`.
The CLI flag `--attestation-class engine_declared_workload_observed` is valid
only with `--run` (without it there are still zero effects); there is no
environment variable, config-file default or alias. Platform validation gets
the same explicit flag, and its default stays strong-only.

## Test plan (offline, synthetic, no default Docker contact)

All tests use the existing fakes (`execute`, `observations`, a stubbed `run`).
None contacts Docker, the network or a provider.

- **Digests.** The golden digest of a fixed v1 plan, captured on the base
  commit, is unchanged. A weak plan's digest and labels differ, and an unknown
  profile is rejected.
- **Selection.** The default config is strong. A config/plan class mismatch
  blocks before launch with no process spawned. The CLI default output is
  unchanged, and the weak flag without `--run` has zero effects.
- **No fallback.** Under the strong class, a preflight failure blocks with
  `daemon_host_namespace_observer_unavailable` and never calls the
  engine-declared identity.
- **Weak verifier.** A complete synthetic observation yields
  `workload_observed` with the class fields, never `verified`. Every existing
  workload-side fault also blocks under the weak class, as do a
  `container:<id>` peer, a changed engine snapshot, a command-digest mismatch
  and a self-namespace versus `/proc/1` mismatch.
- **Strong path.** `tests/test_local_docker_namespace.py` passes unmodified.
- **Supervision parity.** Parametrize `supervise` over both classes for
  success, failure, cancel, deadline, broker-loss, guardian-preclaimed and
  `recover`. Destructive-call sequences, exact IDs and termination and cleanup
  flags must be identical; only observer calls differ.
- **Journal.** `report()` exposes the class. A tampered class event, or one
  that differs from the plan, raises `JournalConflict`.
- **Harness.** Feed `_fixture` a synthetic `summary.json`, with `_sh` and
  `_docker` monkeypatched. A weak run gives weak-labelled PASS rows and leaves
  strong rows as GAP. A requested/recorded class mismatch, or a missing class
  field, is FAIL.

## Platform-validation reporting

Case IDs carry the class, for example
`network-none-success[engine_declared_workload_observed]`. The class is read
from the journal receipts, never from the harness's own flag.

PASS is issued only for the recorded class; strong-class rows stay **GAP**
with the observed gate. `stop-cleanup[...]` PASS is labelled "engine-observed
termination/cleanup", and independent watchdog termination remains a stated
gap. A summary row reads, for example: "1a: PASS (engine-declared,
workload-observed; not independent) / GAP (daemon-host independent)".

## Open questions for the human (resolved above)

1. **Amend namespace-proof?** Do you accept amending its "Remaining
   prerequisite" paragraph to allow a separately named class? The prohibition
   on evading the *strong* gate stays.
2. **ADR 0033 freeze.** Is this local-fixture change an allowed exception to
   the freeze on ordinary-docker and the witness planes, or does it wait for
   Outcome 1 Gate A?
3. **Linux hosts.** May operators choose the weak class where the strong
   reader works? The draft says yes ("operator choice"), to avoid an implicit
   fallback coupling.
4. **Closing the gaps.** Does a weak-class PASS close items 1a and 5, or only
   annotate them?
5. **Naming.** Are the class names and `observer_independence` values
   acceptable?
6. **Strong-class re-check.** Should rule 5's paused re-check also apply to
   the strong class? That changes behavior and needs its own tests.
7. **Live run.** Is a live weak-class run (one bounded invocation, no retry)
   authorized separately from implementation?

## Kernel fallback tunnel devices

A direct probe on a Docker Desktop (LinuxKit) host with `--network none` showed that
the kernel creates fallback tunnel devices in every new network namespace:
`socket.if_nameindex()` returned `lo, tunl0, gre0, gretap0, erspan0, ip_vti0,
ip6_vti0, sit0, ip6tnl0, ip6gre0`. All but `lo` are administratively DOWN with no
IPv4 or IPv6 address; `/proc/net/route` has no rows, `/proc/net/if_inet6` lists only
`lo`, `/proc/net/ipv6_route` only `lo` routes, and `/etc/resolv.conf` names a
nameserver that is unreachable because no non-loopback interface is up. The strict
`names == ['lo']` predicate therefore rejects a genuinely networkless container
(workload exit 78, check `network`), and the observer's loopback-only predicate would
reject the same namespace next.

**Weak class only.** `local-networkless-fixture-engine-attested-v1` uses a distinct
program text and observer (both part of its package digest) and the weak verifier
passes an explicit rule to the shared predicates. `lo` is checked exactly as before.
Any other link is accepted only if its name is in the finite reviewed constant
`KERNEL_FALLBACK_TUNNELS` (the nine devices above), it is DOWN (no `IFF_UP`), and no
IPv4/IPv6 address or route references it. Any other name, an UP, addressed or routed
fallback device, duplicate names, a missing `lo`, or a sysfs name set that is not
equal to the netlink name set (and within `{lo}` plus the constant) fails closed with
a distinct finite gate (`unexpected_interface`, `fallback_interface_up`,
`fallback_interface_addressed`, `fallback_interface_routed`, `duplicate_interface`,
`loopback_missing`, `ambiguous_sysfs_visibility`). The in-workload program proves the
same with stdlib only (`if_nameindex`, `SIOCGIFFLAGS`, `SIOCGIFADDR` must fail for
non-`lo`, and the three `/proc/net` tables).

**Diagnostics.** A failed weak `network` check emits a bounded `network_diag` in the
`limits_unknown` record: per-interface name (reviewed names or `other`), index, UP
flag, IPv4/IPv6 address counts and route-row counts, link count, and only the
*count* of `/etc/resolv.conf` nameserver lines (never addresses or text), at most 1 KiB.
`log_records` accepts it only for the weak profile, only on the `network` check, with
strict key/type/size validation. A weak verifier rejection also journals a bounded list
of sanitized interface names beside the finite gate.

**sysfs `bonding_masters`.** A live run then stopped at `ambiguous_sysfs_visibility`:
`/sys/class/net` also lists `bonding_masters`, a regular control file created by the
bonding module, not an interface (real interfaces there are symlinks). The weak
observer therefore classifies each entry with `os.lstat` semantics (no following):
symlinks form `sysfs_interfaces`; everything else is reported in `sysfs_other` as
`{name, kind}` with kind `file`, `dir` or `other`. The weak rule requires
`sysfs_interfaces` to equal the netlink name set (within `{lo}` plus the fallback
constant) and every `sysfs_other` entry to be a regular file named in the finite
reviewed constant `SYSFS_NON_INTERFACE_FILES` (`bonding_masters`). A directory, another
file name, or a non-symlink named like an interface fails closed with
`ambiguous_sysfs_visibility`; a rejection retains a bounded, sanitized `sysfs_other`
list beside the interface names. The strong rule and `OBSERVER` are unchanged.

NOTE (human decision needed): the strong `daemon_host_independent` class keeps the
strict `names == ['lo']` rule and byte-identical v1 program, observer and digests.
It may therefore reject native Linux hosts that create fallback tunnel devices.
Whether to refine the strong class the same way is a separate, explicit decision.

## Implementation notes

Implemented as steps 1-5, 7 and 8 of the builder plan (the platform-validation
harness changes are owned elsewhere):

* The class derives from `FixturePlan.profile`; `local-networkless-fixture-v1` plans
  are byte-identical (golden digest test captured from the unmodified code).
  `LocalFixtureConfig.attestation_class` defaults to strong; a plan/config mismatch
  raises `BackendBlocked` in `launch` (and again in the supervisor child) before any
  filesystem, process or engine effect, and an engine refuses a plan of another class.
* `EngineDeclaredIdentity` and `verify_engine_declared_observation` (weak) share the
  in-workload predicates verbatim with `verify_observation` (strong).
* `supervise` records `attestation_class` and `observer_independence` in
  `network_preflight` and `network` events (including unknown/blocked ones);
  `report()` exposes them and raises `JournalConflict` for a recorded value that
  differs from the plan-derived class (a weak plan has no legacy no-class excuse).
* The CLI summary state for a weak run is `passed_weak_attestation`, not `passed`.

## Live result and output-capture finding (Docker Desktop, weak class)

Bounded live runs on this host (Docker Desktop, Engine 29.7.2, LinuxKit, local
`python` image, no pulls, no network, no spend) established, in order:

1. The workload's strict `names == ['lo']` check failed (exit 78) because the
   kernel creates its fallback tunnel devices in every namespace; fixed for the
   weak class only (see "Kernel fallback tunnel devices").
2. The weak observer then stopped at `ambiguous_sysfs_visibility` because
   `/sys/class/net` also lists `bonding_masters`, a regular control file; fixed
   for the weak class only (`SYSFS_NON_INTERFACE_FILES`, type-checked).
3. With both fixed, the weak network proof reaches `workload_observed`
   (loopback-only addresses, no fallback device up/addressed/routed, engine
   `NetworkMode=none` and no peer sharing the namespace).
4. The `cancel`, `timeout` (deadline) and `broker-loss` (disconnect) cases then
   completed with termination and exact-ID, non-force cleanup confirmed and no
   container left behind: **PASS (weak attestation)**. `timeout` and
   `broker-loss` end in the fixture's designed `unknown` terminal state with
   confirmed termination and cleanup; that is the contract, not a pass of the
   workload.
5. The `success` and `failure` cases first ended `unknown`
   (`lost_reply_or_invalid_evidence`): `docker cp` of the `/output` tmpfs mount
   returns an archive of an empty directory on this engine, although
   `docker exec` shows `result.txt` inside (verified with a direct probe for both
   `--tmpfs` and `--mount type=tmpfs`). That output-capture bug is independent of
   the attestation class. `main` now backs `/output` with a labelled
   tmpfs-typed local volume; with it, **all five cases pass under the weak
   class** (PASS-WEAK, no container or volume residue).

The `--cases` option of `scripts/check_local_docker_fixture.py` selects an exact
subset; a subset can only report `partial_passed*`, never a full pass.

