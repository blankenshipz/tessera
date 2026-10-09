# Local networkless Docker Desktop fixture

The additive [independent supervision gate](independent-sandbox-supervision.md)
provides a separate opt-in guardian adapter and one-case command. The current
network probe queries namespace interfaces rather than sysfs; the one new live
attempt still blocked before release. Historical observations below remain history,
not retroactively upgraded evidence. The original CLI remains separate.
The [namespace-proof follow-up](local-docker-namespace-proof.md) now requires
independent daemon-host namespace identity as well as namespace-local rtnetlink
and connectivity observations. The macOS/LinuxKit boundary currently fails this
preflight before any container creation; no prior evidence is upgraded.

This is an **explicit opt-in, closed harmless conformance fixture**, not a general
execution backend, capability activation, package installer, downloader, or
scientific experiment. Kubernetes activation and provider-disabled defaults are
unchanged. `LocalFixtureAdapter.preflight` always rejects broker download/install
authority, including when the local fixture succeeds. No isolated control package
is issued and no control receipt, project artifact release, or result is invented.

## Runbook

Use the existing Python environment; no installation is necessary or performed.
Default invocation does not inspect Docker, read credentials, create directories,
spawn a supervisor, or create a container:

```sh
.venv/bin/python scripts/check_local_docker_fixture.py
# {"state": "disabled", "effects": 0, "activation": "blocked"}
```

Only with authorization for local Docker effects, choose an **already local,
reviewed Python image**, with `/usr/local/bin/python` and cgroup-v2 support. The
image must not contain credentials. Image identity checks cannot establish the
absence of secrets baked into arbitrary image layers. No build, pull, login,
tagging, image deletion, dependency installation, or provider calls are available.
`tag@digest`, bare tags, credential-bearing references, and registry-port variants
are deliberately outside this narrow reference grammar.

Set `EVIDENCE_DIR` to a new private directory outside the repository before running
the opt-in case; it must be empty and owned by the invoking user.

```sh
.venv/bin/python scripts/check_local_docker_fixture.py --run \
  --image 'reviewed-local-image@sha256:EXACT_64_LOWERCASE_HEX_DIGEST' \
  --evidence-dir "$EVIDENCE_DIR"
```

The evidence destination must be a new absolute canonical path under an existing
parent (on macOS `/tmp` is a symlink; use `/private/tmp`). It becomes private 0700.
The script runs at most five sequential cases: small declared output/success,
the same output with deliberate exit 7, cancellation, absolute deadline, and
broker disconnect. Each gets fresh fixture-only operation/nonce/package identity,
one create, and one start at most. It stops the suite on unexpected state or
unconfirmed cleanup. An unsuccessful invocation is not permission for an automatic
retry with a new identity. Retain and investigate its original evidence first.

## Frozen boundary

* Explicit `LocalFixtureConfig(enabled=True)`, separate from `RealBackendConfig`,
  Kubernetes, `CpuRuntime`, and its development-output mount. Only the local Unix
  socket `unix:///var/run/docker.sock` is supported. Docker Desktop/Linux/cgroup-v2
  readback is required. The Docker CLI has a private empty config and a minimal
  host environment, no ambient context, Docker credentials/helpers or proxies.
* The immutable repository digest must occur in local `RepoDigests`. Creation uses
  the inspected immutable image ID with `--pull never`; image volumes, OnBuild
  hooks and unexpected environment keys are rejected. The entrypoint and command
  are fixed. The program digest, image, limits, mode, predeclared constant input,
  tenant/project/worker fixture identities, operation and nonce bind the package
  digest. This is a **fixture package**, not a signed control capability package.
* UID/GID 65532, read-only root, ALL capabilities dropped, no-new-privileges,
  default seccomp (runtime checks filter mode), no host PID/IPC/UTS, network `none`,
  no restart policy or healthcheck. Image and container identity/security/command
  are inspected before start. There are **zero host bind mounts**, including no
  socket, home, credential, control-state or control-PVC mount. Inputs are only the
  fixed harmless constant in the reviewed program; arbitrary input staging is not
  offered. Standard Docker kernel/pseudo-filesystem mounts are not host data access.
* CPU 250 millicores, memory 128 MiB including swap disabled, 32 PIDs, no core dumps,
  64 file descriptors. The `local` log driver requests one 32 KiB file with
  `compress=false` (its default compression is incompatible with `max-file=1`);
  exact log settings are verified before start. Scratch `/scratch` and output `/output` each request a
  1 MiB tmpfs with 32 inodes, `nodev,nosuid,noexec`, UID/GID 65532 and mode 0700.
  A fixed pre-workload probe checks actual cgroup bounds, non-root/capability/
  seccomp/read-only/network readback, statvfs capacity and actual ENOSPC byte and
  inode exhaustion on **both exact mounts**, then removes its probe files.
  A validated `ready` transcript plus independent namespace proof and unexpired
  supervisor authority are required for workload release. Unsupported or
  unproven limits remain unknown and block that release. Docker flags alone are
  not a hard filesystem-quota claim. This is not persistent storage and does not
  establish byte/inode quotas on arbitrary host volumes or every kernel mount.
  Failed probes retain a finite `check` identifier and `failure` category, not
  exception text, paths, interface names or arbitrary kernel readbacks. These
  diagnostics never authorize release; legacy undifferentiated unknown records
  also remain blocked.
* No arbitrary argv, shell, network, installs, plugins, model APIs, cloud secrets,
  paid providers, Salad, or training. The three closed program modes are `success`,
  `failure`, and `wait`. Only `/output/result.txt` with the exact 28 declared bytes
  can enter custody. This closed byte policy avoids pretending a heuristic secret
  scanner can make arbitrary logs/output safe.

## Supervision, uncertainty and restart

`spawn` creates a separate, non-daemon supervisor process/session before any Docker
effect. Its own monotonic clock arms a fixed 3–15 second deadline (CLI: 10 seconds)
before create/start. A private pipe carries HMAC-authenticated, sequential messages
bound to package and nonce; the workload receives neither the key nor the pipe.
The supervisor owns the journal and Docker connection, not a broker callback thread.
Missed heartbeats for three seconds, invalid/replayed messages, disconnect,
cancellation or the absolute deadline fence further work and request kill.
Heartbeats cannot extend the deadline. Each Docker call has a two-second timeout
and streaming byte cap; pre-effect daemon discovery alone allows five seconds.
The program also has a fixed post-probe 18-second self-exit backstop; it is not
counted as independent supervision.

Deadlines are kill-request deadlines, **not a guarantee that an unavailable Docker
daemon/kernel has terminated a container at that instant**. In-flight bounded calls
and kill/readback add latency. Durable unknown precedes kill; only a fresh exact-ID
Docker inspect can confirm it is not running. Broker loss, deadline or lost replies
remain unknown execution outcomes even after a separate termination observation.
A kill acknowledgement, process exit, timeout or connection failure alone is not
termination evidence. Supervisor/host failure is not tolerated by a second daemon;
it requires explicit recovery. No continuously running service is installed.

After a crashed supervisor has released its exclusive journal lock:

```sh
.venv/bin/python scripts/check_local_docker_fixture.py --run \
  --recover "$EVIDENCE_DIR/$CASE_ID"
```

Recovery reads the same immutable plan/intent/receipts, marks interrupted work
unknown and can perform one separately journaled exact-owned stop/cleanup attempt.
It never calls create/start, changes the nonce, renews authority, reruns the work,
or relabels an unknown execution as successful. Repeating an already confirmed
recovery reads identical evidence without Docker effects. A lost recovery reply
remains unresolved, with no automatic destructive retry; independently reconcile
the recorded exact identity before further work. Opening an in-use journal fails.
Deleting journals or copying operation identities into a fresh root is not supported
recovery; fixture-local dedup is not an authenticated control/global dedup service.

## Custody and cleanup

This adapter uses `CapabilityJournal`'s private owner lock, no-symlink custody and
synchronous-FULL SQLite transactions, with separate append-only local fixture
tables. It does not change broker receipt schemas, normal effect selection or the
control database. Each evidence root admits only one operation. Receipts hash-chain
the package, operation, tenant, project, nonce, timestamps, and observed status.
Duplicate receipt keys return original evidence; conflicting receipts/bytes fail.

Docker tmpfs disappears on container stop. The fixed workload writes its declared
file, emits a bounded `done` record, and waits. The supervisor pauses all container
processes and observes `Paused` before `docker cp CONTAINER_ID:/output -` into a
bounded in-memory tar reader. Nothing is extracted on the host. Traversal,
undeclared paths, symlinks/hardlinks, devices, FIFOs, PAX headers, wrong bytes or
oversized output are rejected. One transaction seals content and its hashed exact
manifest into `quarantined_unreleased` fixture custody with no overwrite. Then
the container is resumed, released to exit, and its actual exit code is inspected.
Failure output is quarantined too. Unexpected output/log/error text is never
printed or stored; errors retain only a hash and finite safe categories. Validated
fixture logs, exit/status, image and container identities remain in the journal.

Termination and cleanup are independent records. Removal requires exact ownership
labels, image, name and immutable ID plus non-running readback. It uses `rm ID`,
never force removal, prune, volume/image deletion, broad filters or namespace
cleanup. A successful complete container listing then confirms the ID is absent;
an inspect error is not absence. No host workspace/input/output mount exists to
delete. Empty private CLI configuration and SQLite/JSON evidence directories are
retained intentionally, not silently pruned. Pipes/process handles are closed and
the parent joins the supervisor. Uncertain operations retain evidence for recovery.

## Checks and observed live limitations (2026-09-23 UTC)

Offline tests are in `tests/test_local_docker_fixture.py`. They use fake Docker,
temporary journals, synthetic archives, and real harmless local child processes
for independent-clock, subprocess output-cap and timeout checks. None contacts
Docker or a provider. They cover mutable references, security/readback/resource
bounds, network none, identity/ownership, path/secret rejection, success/failure/
cancel/unknown, lost create/start/kill/capture replies, restart, custody/idempotency,
cleanup and blocked activation. Existing CPU, capability broker/journal, activation,
watchdog and artifact-custody tests remain separate regressions.

Final targeted verification on the existing dirty `main` tree:

```sh
.venv/bin/pytest -q tests/test_local_docker_fixture.py tests/test_cpu.py \
  tests/test_runtime_capability_broker.py tests/test_runtime_capability_journal.py \
  tests/test_capability_activation.py tests/test_capability_watchdog.py \
  tests/test_artifact_custody.py
```

**340 passed**, two existing Starlette/httpx/anyio deprecation warnings, 72.20s.
Scoped Ruff lint passed; Ruff format check reported all five new Python files
formatted. `git diff --check` and individual no-index whitespace checks for all
six new files produced no whitespace findings. Default CLI again returned disabled
with zero effects. A final read-only label-filtered Docker container listing
returned no fixture containers. Full-repository pytest/lint/format integration
gates were not run; these results are targeted acceptance, not integration readiness
or successful live conformance. Existing unrelated edits were preserved.

Local Docker Desktop 29.7.2, Linux/cgroup v2 and the already present image
`tessera-control-local@sha256:facbb6eda289f9e663a52f79abe1d0002ebab93bfb98046b00461206f140556c`
were observed. The bounded live suite **did not pass**:

* Local attempt ID `NETWORKLESS-ATTEMPT-A-2026-09-23`: pre-effect discovery
  exceeded its original two-second allowance. No create intent/container exists.
  Discovery was narrowed to three non-secret fields and given a five-second bound.
* Local attempt ID `NETWORKLESS-ATTEMPT-B-2026-09-23`: Docker created container
  `86501fcc10bc881de1bba6d27e53d8719c0692ca09e95e9ff138641328f6ed02`, but start
  returned an error. Readback showed `created`, not running, exit status 128.
  Exact-ID removal and subsequent successful listing confirmed cleanup.
* Local attempt ID `NETWORKLESS-ATTEMPT-C-2026-09-23`: a bounded diagnostic attempt retained the sanitized engine-error digest
  `0a42d7da1594e76b554d61efc00051df13e55733b0d0205efb057703e485b3ac` (no recognized
  safe error category). Container
  `46e5805f6ac834ca2ad3fbea17904c56eafc6788e53814a5d8e859f62be26588` was likewise
  confirmed non-running and removed. Evidence digest:
  `4bdaaa797cac231cec3d88a9d8b4d62dbf3b8b373d97591909c31617e87323c4`.

These were explicit development attempts, not automatic operation retries. All
live attempts stopped before workload release. **No live filesystem enforcement,
output capture, successful/failed command, cancellation or broker-loss kill is
proven.** The last two execution outcomes remain unknown; termination and cleanup
are separately confirmed. The next live prerequisite is a separately investigated
Docker start refusal for this exact restricted configuration. Do not weaken the
profile or claim quota enforcement to obtain a passing demonstration. Evidence
stays outside Git; no images were downloaded/built, credentials accessed, control
or Kubernetes resources changed, or scientific work performed.

### Start-refusal diagnosis and single rerun (2026-09-23 UTC)

The original refusal is now identified, not a demonstrated inability to apply the
sandbox restrictions. Docker Desktop's host-retained VM log
`~/Library/Containers/com.docker.docker/Data/log/vm/init.log`, lines 3467 and 3551
at inspection time, records HTTP 500 for both exact container start requests:

```text
failed to create task for container: failed to initialize logging driver: compression cannot be enabled when max file count is 1
```

SHA-256 of that exact text is
`0a42d7da1594e76b554d61efc00051df13e55733b0d0205efb057703e485b3ac`, matching
attempt `-c`'s retained `State.Error` digest. The minimal fix explicitly disables
log compression without increasing the log file count/size or relaxing any
sandbox restriction. Regression tests check rendering and reject missing/enabled
compression or a changed file count in readback. The prior three read-only SQLite
receipt chains matched their summaries; none contained output custody.

Read-only preflight observed Docker Desktop 29.7.2, Linux 7.0.12-linuxkit, cgroup v2,
memory/swap-limit support and built-in seccomp. The same already-local digest image
is Linux/amd64, UID/GID 65532, no image entrypoint/volumes/OnBuild hooks; its original
service command is replaced by the unchanged fixed fixture Python program.

Exactly **one** authorized `success` case was run, not the five-case CLI suite.
It used a private diagnostic helper not shipped in the source distribution (local
record ID `NETWORKLESS-DIAGNOSTIC-SUCCESS-2026-09-23`). Evidence root is not a
repository path or package input.
`preflight.json` records host/image discovery; `success/docker-observations.jsonl`
retains bounded rendered commands and inspect readbacks; `success/broker.sqlite3`
and `summary.json` retain the receipt chain; `verification.json` records independent
evidence checks and a fresh complete-list absence observation.

* Container: `6f76ac26838cec3f68c6ef90cd58a8465fe13b0d40f29cb65d66d788d5b63b19`.
* Operation: `c907f3c4d050a2bd7e00b3b4115725edf8299ae79e13f7d9d5a473b10f31431a`.
* Evidence digest: `46ccabd0bc71214fccafa0ff6c202312c81419d4ee4e213a722013968fe12843`.
* One create and one start succeeded. Separate inspect observed `running` (VM PID
  31992), then `exited`, exit **78**, no Docker `State.Error` and no OOM kill.
* All retained inspect security settings passed verification, including non-root,
  read-only root, network none, dropped capabilities, no-new-privileges, zero host
  binds, exact tmpfs/resource/ulimit settings, and the fixed log configuration.
* The pre-workload program emitted **`limits_unknown`**; it did not emit `ready`.
  The supervisor correctly withheld release. Outcome is **blocked before workload**,
  not completed, failed scientific work, or cancelled. Actual runtime-limit
  enforcement remains unproven: the existing finite error transcript does not identify
  which assertion failed. No output/artifact digest or custody receipt exists.
* Supervisor PID 16713 exited and was joined. Exact non-running readback preceded
  non-force removal; fresh Docker listing confirmed absence and zero fixture
  containers. No kill was needed after exit 78. Live cancellation/deadline killing
  and output capture were not exercised. There was no second rerun.

The same targeted seven-file pytest command above now reports **343 passed**, two
existing deprecation warnings, 97.04s. Scoped five-file Ruff lint/format checks and
whitespace checks passed; default CLI remains disabled with zero effects. Full
integration checks were not run. Capability activation remains blocked; diagnosing
the finite runtime-limit failure is a separate prerequisite, not permission to
weaken the profile or run another container.

### Restriction diagnostics and one diagnostic rerun (2026-09-23 UTC)

Attempt `-d` cannot be retrospectively attributed to an exact assertion. Its
preflight/verification, 16 Docker observations and 12-event SQLite receipt chain
were reinspected. The actual log and journal `limits` observation have only
`stage`, `nonce` and `package`; the original program caught every probe exception
and emitted the same `limits_unknown`. There is no traceback, assertion identifier
or observed kernel value to recover. Docker inspect configuration is not proof of
the values seen by the in-container probe. The receipt chain and original package
digest were independently verified using the **retained original program**, not
the newly instrumented program. The historical evidence was not rewritten.

The minimal diagnostics fix labels every restriction and distinguishes assertion
failure from missing files, permissions, ENOSPC, read-only errors, other OS errors
and other exceptions. The parser accepts only that finite protocol and exact
nonce/package binding. All checks, exit 78, workload-release gate, Docker arguments,
supervision and custody remain in place. No permissive fallback was added.

After offline checks and a fresh read-only preflight passed, exactly one `success`
case was run using the same local pinned image and unchanged sandbox configuration.
The private diagnostic helper and evidence are indexed as local record ID
`NETWORKLESS-DIAGNOSTIC-LIMITS-2026-09-23`, containing
preflight, exclusive one-run intent, summary, verification, Docker observations and
the broker journal. The one-run harness is outside the repository/default CI.

* Container: `414721a6f74532e82954f3425c2c294a5e93de7905234e05568994a8dd45899a`.
* Operation: `4fe9058c669a0722f3145e34b9a66aab2ccd1811c4eaafc421d43ab6114864fb`.
* Evidence digest: `795c6275ebf89e93d5a5e82f807e852691e30611299e0d3b03ee574de1775fd3`.
* The exact diagnostic is `check="network", failure="assertion"`. It identifies
  `assert sorted(os.listdir('/sys/class/net')) == ['lo']`. UID/GID assertions
  preceded it and passed. The directory read returned, but its result was not
  exactly `['lo']`. Interface names were intentionally not retained. This is not
  evidence of actual network connectivity, nor proof that attempt `-d` failed at
  the same assertion. The underlying Docker Desktop namespace/sysfs discrepancy
  is not established by these observations.
* Inspect still verified network `none` and the rest of the requested security
  settings. The runtime network witness did **not** pass; subsequent capability,
  seccomp, cgroup, read-only-root and tmpfs-exhaustion probes were not reached.
  Their hard-limit enforcement is therefore still unproven in this live run.
* One create/start, observed running then exited 78, no OOM or engine error, no
  `ready`, workload release, `exec`, artifact capture or output custody. The outcome
  remains **blocked**. The independent supervisor exited and was joined. Exact
  non-running readback preceded one non-force removal; fresh complete and
  fixture-filtered listings confirmed absence and zero fixture containers.
* There was no second retry, provider/model/Salad/download/install effect or
  sandbox relaxation. Capability activation remains blocked.

Targeted verification: the prior seven-file pytest command plus
`tests/test_local_docker_limits.py` reports **377 passed**, two existing dependency
deprecation warnings, 89.28s. The new tests execute the actual fixed probe against
synthetic OS/filesystem reads for every check and failure category; they verify
that missing limits cannot be waived, arbitrary diagnostic text is rejected, and
the supervisor retains the diagnostic without release/custody. An initial test
double incorrectly used `0` as a failing swap value (the required value is `0`);
that test-only defect was corrected before the passing run. Scoped six-file Ruff
lint/format and whitespace checks pass; the default CLI remains disabled with zero
effects. These are targeted checks, not full integration acceptance or successful
live conformance. Further runtime investigation needs a separately authorized
bounded attempt; no remaining authority from this one-run task permits a retry.
