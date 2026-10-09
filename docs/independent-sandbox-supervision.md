# Independent sandbox supervision — local fixture only, activation blocked

[Decision 0021](decisions/0021-independent-sandbox-supervision.md) records candidate
research, local inventory, selection, safety review and acceptance gates. No
Kubernetes sandbox is installed or selected for deployment. Agent-sandbox and
OpenSandbox are orchestration candidates, not evidence of an enforcing target.

The [namespace-proof follow-up](local-docker-namespace-proof.md) verifies which
predicate each historical package used and adds independent daemon-host identity
plus bounded namespace-local observations. Its single authorized invocation
blocked **before create**, with zero case attempts: the macOS supervisor cannot
read LinuxKit's procfs namespace identities. Historical results below are unchanged.

## Command and authority

```sh
.venv/bin/python scripts/check_sandbox_supervision.py
# {"state": "disabled", "effects": 0, "activation": "blocked"}
```

Default mode does not inspect Docker or credentials, create state, or spawn a
process. With separate authorization for one local fixture effect:

Set `EVIDENCE_DIR` to a new private output directory outside the source tree, then run:

```sh
.venv/bin/python scripts/check_sandbox_supervision.py --run \
  --image 'ALREADY_LOCAL_REVIEWED_IMAGE@sha256:EXACT_DIGEST' \
  --evidence-dir "$EVIDENCE_DIR"
```

At most one fixed `wait` case, only after read-only preflight, one create/start
maximum, no retries. Preflight evidence lives at the root; a launched case uses
`case/`. An unavailable independent namespace observer records the exact gate with
zero attempts. The program
ignores SIGTERM and performs the existing pre-release restriction probes before
waiting. It has no shell, arbitrary input/argv, external service traffic, package/model/
provider/Salad effects or declared output. Never substitute a mutable image or
weaken a probe to get a pass. No image pull/build/install occurs.
The trusted network observer's bounded loopback controls and documentation-address
negative probes are detailed in the namespace-proof follow-up.

`GuardedFixtureAdapter` is opt-in and inherits the unconditional real-capability
preflight rejection. This is an adapter seam plus closed conformance implementation,
**not** an authenticated autonomy backend. Do not copy local fixture receipts into
the control broker's receipt API. Session approval, this CLI and vendor readiness
cannot issue an isolated package or global operation claim.

## Supervision and receipts

The broker spawns a detached guardian; that process claims the immutable plan,
records arming and spawns/owns the separate supervisor. All Docker effects still
belong to the supervisor until it exits. A private challenge/ack pipe uses distinct
directional HMAC keys, exact nonce/package and consecutive sequence. Only one
challenge is outstanding, avoiding an unbounded buffered heartbeat or blocking
pipe-fill loop. There is no workload-visible signing key or control mount.

The supervisor checks guardian liveness before/after forward Docker calls and
before broker messages. Loss fences forward work while leaving stop-only cleanup
available. The guardian enforces a six-second peer lease and an absolute
`runtime_seconds + 12` deadline from its own monotonic clock, armed before the
first challenge can release Docker work. The supervisor's existing three-second
broker lease and ten-second CLI absolute deadline remain unchanged. Heartbeats
cannot extend either absolute deadline. Kill request, confirmation and cleanup
have distinct meanings; these are not guarantees against kernel/daemon failure.

A hung supervisor is SIGKILLed through its owned child handle (never a caller PID).
Only after confirmed child exit and exclusive journal lock acquisition can the
guardian take over the **first** exact nonce/image/container-ID stop attempt. The
guarded supervisor must durably claim stop before dispatch; `kill_pending` counts
as a consumed stop even if a later stop receipt is lost. A failed durable claim
defers that first attempt to the guardian; unavailable storage stays unknown.
The legacy single-owner fixture's emergency kill behavior is unchanged.

Existing stop/recovery/guard intent forbids automatic destructive retry. An
uncertain stop can therefore remain unresolved even with a working guardian.
Repeated guardian reconciliation reads the same receipt, never creates, starts,
renews, or retries work. Cleanup does not require current workload authority: it
uses historical exact ownership only, after confirming non-running state. No
namespace deletion, `rm -f`, broad selector destruction, image/volume deletion,
or journal deletion is permitted. Private evidence/config directories are retained.

`FixtureJournal` retains package/tenant/operation/nonce-bound, hash-chained,
monotonic/UTC-stamped events and original quarantined-byte rules. Hash chains and
private local pipes are not remote attestation against a compromised host user.
Historical journals bind the exact program digest: retain the original program/
source with old evidence; a changed program must not be substituted to rewrite or
re-execute an old operation. No control database schema or receipt format changes.

## One live attempt: blocked, exact cleanup confirmed

2026-09-23, 14:48 UTC, exactly one invocation of the command above with the reviewed
local image from decision 0021. Exit status **1**, `state=blocked`, attempts **1**.

* Local evidence ID: `SANDBOX-SUPERVISION-ONE-ATTEMPT-2026-09-23`.
  `broker.sqlite3` contains the 14-event receipt chain; `supervision-summary.json`
  contains the verified report. No secrets or runtime state were added to Git.
* Operation: `367d8f75fe2d034d178bae61237aff85221903d59c9cbf40554f411716094fdb`.
* Nonce: `848e10691b5bcbd1421afae0afae6ea26c56a9683c9a5a95055b4d7927619659`.
* Package: `ff130a62434dc83516ca2d76a50cf2075d211f288073ea83d1f2b165d508260a`.
* Container: `212cc05d239679914733e5a895cd3c528f560b08ef8d2c33dca5a04e782bd017`.
* Evidence digest: `2147bfd44f27c4104eedb9cb80d3a615b2ba48201e6342671ad6923bf5179c1a`.

The current runtime probe uses `socket.if_nameindex()` (network namespace query),
not `/sys/class/net` (mount-dependent view). It returned a result other than exactly
loopback, producing `check=network`, `failure=assertion`. Names were intentionally
not retained. This does **not** prove external connectivity or identify why Docker's
`network=none` readback disagrees with the narrow probe. It does establish that
changing the observer alone did not resolve the gate. UID/GID checks passed;
subsequent capability/seccomp/cgroup/read-only/tmpfs probes were not reached.

There was one create/start, exit **78**, no `ready`, no workload release, no artifact
capture/custody and **no live watchdog-kill proof**. Exact non-running readback
preceded removal; the successful complete listing confirmed absence. A separate
read-only complete listing afterward returned only two unrelated containers,
neither the recorded ID; the fixture-filtered listing was empty. Guardian and
supervisor exit were observed, but peer-channel closure is not a diagnosis of a
supervisor crash. No second live attempt or repair/retry was made.

## Regression evidence

Default tests never contact Docker/Kubernetes. Tests cover:

| Case | Evidence |
| --- | --- |
| Agent hang / broker dies / cancellation / deadline | Existing fake-engine fixture cases; actual separately running supervisor clock; new real-pipe EOF tests |
| Watchdog/guardian dies | Authenticated guardian pipe loss makes independently running supervisor stop fake workload |
| Supervisor hangs, ignores SIGTERM | Real child process ignores TERM; independent guardian sends KILL, joins, acquires released journal and exact-stops fake container |
| PID reuse / container identity mismatch | No raw PID input; reaped child handle cannot invoke `os.kill`; existing all-binding ownership mutation tests reject destructive calls |
| Lost replies, unknown, no duplicate effect | Create/start/kill/capture failures; guard lost stop/cleanup receipts; prior stop claim not redispatched; no create/start in reconciliation |
| Stale/replayed evidence | Exact directional key, nonce/package, sequence and expired queued-message rejection; existing activation/witness freshness/replay tests |
| Cleanup after authority expiry | Historical stop-only reconciliation, exact identity and exclusive-owner checks; no authority renewal |
| Custody and limits | Existing archive/path/link/secret/quota/readback tests plus actual fixed program against synthetic namespace/kernel/filesystem observations |

The broad targeted command:

```sh
.venv/bin/pytest -q tests/test_local_docker_guardian.py \
  tests/test_local_docker_fixture.py tests/test_local_docker_limits.py \
  tests/test_autonomy.py tests/test_runtime_capability_broker.py \
  tests/test_runtime_capability_journal.py tests/test_capability_activation.py \
  tests/test_capability_watchdog.py tests/test_capability_kubernetes_runner.py \
  tests/test_capability_collector.py tests/test_capability_runtime_witnesses.py \
  tests/test_capability_witness_live.py tests/test_artifact_custody.py
```

**1112 passed**, two existing Starlette/httpx/anyio deprecation warnings, 468.46s.
An initial invocation hit the 120-second tool timeout without a test failure; the
completed invocation used a 600-second timeout. Afterward, the guarded durable-stop
claim edge case was tightened and two regressions added; final scoped checks are
recorded below. No live rerun followed that fail-closed change.

Final `.venv/bin/pytest -q tests/test_local_docker_guardian.py
tests/test_local_docker_fixture.py tests/test_local_docker_limits.py`: **157 passed**,
31.77s. Scoped Ruff lint passed and format check reported **6 files already
formatted** (guardian, fixture, contract, CLI and two affected test modules).
`git diff --check` and individual `git diff --no-index --check /dev/null PATH`
checks for the affected untracked code/tests/docs passed with no whitespace
findings. Default CLI returned disabled/effects=0/activation=blocked.

## Remaining gates

* Resolve the actual local network namespace discrepancy with separately authorized
  bounded diagnostics. Do not infer network isolation from Docker inspect alone.
* Obtain live post-network limit/exhaustion, ignored-TERM independent kill, broker/
  watchdog loss and quiet/no-continued-writes evidence. This attempt proves none
  of those later gates; offline passing evidence cannot replace them.
* Simultaneous guardian/supervisor or host/daemon failure remains unknown. An
  in-flight Docker CLI/daemon request may outlive a killed supervisor; missing
  exact readback is not absence. This is not a node-native process/cgroup sensor,
  installed OS service, or a guarantee of termination during daemon/kernel outage.
* Deploy/review pinned Kubernetes controllers/runtimes only under separate scope;
  provide native CNI/quota/CRI witnesses, authenticated endpoints/identities and
  admission/defaulting evidence before selecting a Kubernetes implementation.
* Implement authenticated real control-package issuance, global claim/revocation,
  worker/session binding and durable broker receipt integration before autonomy
  activation. Existing synthetic/provider-disabled responses remain unchanged.

These are task-scoped checks, not full repository integration acceptance. The
pre-existing dirty `main` work was preserved; no commits, pushes, dependency
installs, Kubernetes changes, provider/model/scientific effects or downloads ran.
