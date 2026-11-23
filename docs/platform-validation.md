# Platform validation

This report records an opt-in acceptance run of Tessera as a general agent
runtime on one development host (macOS, Docker Desktop). Every harness result is
**PASS**, **PASS-WEAK**, **FAIL** or **GAP** with its evidence; item 6 is a code
fix covered by unit tests. A GAP is an unproven or
unimplemented capability, not a pass: nothing here claims a provider ran, a
spend cap held at a provider, or an experiment was scientifically valid.

Harness: `scripts/platform_validation/run.py` (opt-in, not collected by default
pytest, no paid or network calls). Run `--only restart|spend|lineage|docker|campaign` for
one capability. Each check starts a real `tessera serve` process over loopback
with a temporary state directory and exercises it over HTTP, except the Docker
check, which uses local Docker directly.

Historical counts from harness runs whose raw logs were not retained (removed for private details); they cannot be re-verified from this repository:

| Run | Log | PASS | PASS-WEAK | FAIL | GAP |
|---|---|---|---|---|---|
| Local weak-attestation opt-in only | (raw log not retained) | 96 | 5 | 0 | 16 |
| No opt-in (default) | (raw log not retained) | 95 | 0 | 0 | 21 |
| `--only no_session` on a temporary merge with the grant poller | (raw log not retained) | 6 | 0 | 0 | 0 |

The third run was made on a temporary tree that included the grant poller
(`development-poll`), to show that the worker daemon runs that poller unchanged. Without
a work source the same case reports GAP.

PASS-WEAK is the five weak-class supervised cases (ADR 0035); it is reported
separately and never counted as `PASS`. The six strong-class Docker cases are GAP
on this host. The live-egress cases were not run for these logs (see below), so
their GAP rows read `not run`; their last live result is recorded in sections 1b
and 2 and was produced by the same checks with `TESSERA_PV_LIVE_EGRESS=1`.

Two opt-ins gate effects that are off by default:

- `TESSERA_PV_WEAK_ATTESTATION=1` runs the weak-class fixture on local Docker only,
  after a precondition confirms the tree carries the labelled tmpfs-typed `/output`
  volume (the case FAILs fast otherwise, and is emitted only in weak mode).
- `TESSERA_PV_LIVE_EGRESS=1` runs the live egress and dataset-staging cases, which
  send two HTTP requests per run to public example domains through the
  allowlisted proxy. Without it those cases report GAP and make no worker run and
  no request.

The `real-run`, `receipts-lineage` and in-flight `restart-recovery` checks make real
worker runs on the local image only (nothing is pulled or built).

## Campaign runner check (ADR 0038)

`--only campaign` (`check_campaign.py`, capability `campaign-runner`) is local Docker
only and needs a local `python@sha256:` image (otherwise GAP; never pulled). It starts its
own `tessera serve --campaign-tick-seconds 0.5` and a `tessera-worker development-poll`
process and runs a synthetic stdlib-only payload. Cases: `staged-input` (a later stage
reads an earlier stage's exact bytes), `transient-retried` (a poller limited to one grant
leaves the next grant to expire unleased, then an unbounded poller runs the retry),
`over-budget-refused`, `restart-resumes` (SIGKILL of `serve` mid-attempt), `receipt` (digest
recomputes, run receipt digests, lineage edge, measurement equals bytes, scientific verdict
not a pass), `cancel-running`, `data-class-refused` and `docker-residue`. It is run by the
integrating operator, not by default pytest; results here are operational evidence only.

## Evidence class

Only results produced by a Tessera run (a `tessera serve` process driven over
its public HTTP surface, with recorded events, holds and receipts) count as
platform evidence. This report contains no research or experiment results. The
two cases labelled **direct docker** (`direct-network-none`,
`direct-stop-cleanup`) are plain `docker` commands run outside Tessera: they are
**non-platform, provisional** smoke tests, and they do not count toward items 1
or 5. Closing those items needs a Tessera-run path (see Platform gaps).

## Summary

| # | Capability | Result | Notes |
|---|---|---|---|
| 1a | Sandbox run, network none | **PASS-WEAK / GAP (strong)** | Weak class (ADR 0035): all five supervised cases (success, failure, cancel, deadline, broker-loss) PASS-WEAK on Docker Desktop. Strong class: blocked before container creation on macOS (`daemon_host_namespace_observer_unavailable`) |
| 1b | Allowlisted egress (ADR 0034) | **PASS at the last live run; not validated under the current classifier** | A live worker run under the allowlist profile reached `example.com:80` and was denied `example.org:80`, and the recorded egress journal has the allow and deny rows. That run predates the stricter direct-connect classifier (only `ENETUNREACH`, `EHOSTUNREACH` or a timeout count as blocked; refused or reset is a FAIL). Its observation (`ENETUNREACH`, and a name-resolution failure on direct DNS) is consistent with it, but the case has not been re-run under it: treat 1b as unvalidated for the current code until re-run with `TESSERA_PV_LIVE_EGRESS=1`. Worker-reported journal, not independent proof |
| 2 | Dataset staging run to run | **PASS at the last live run; not re-run** | Depends on the egress run above (opt-in, raw log not retained): run 1 fetched a small file through the allowlist; manifest retained, custody released, staged and released; run 2 read it through input delivery with identical bytes and sha256 |
| 3 | Lineage and receipts (v58) | **PASS / GAP** | Real runs: the observed receipt is posted by the worker and bound to control's own records (operation, plan, argv and manifest digests; tampered receipts refused 409); derived metric, platform-computed gate verdict and go, human-registered lineage join PASS. GAP: no automatic lineage emission, cross-org isolation |
| 4 | Spend controls | **PASS** | Hold, over-cap refusal with rollback, reconciled state; admission accounting only |
| 5 | Stop and cleanup | **PASS-WEAK / GAP (strong)** | Termination and exact-ID non-force cleanup confirmed in each of the five weak-class cases, no container or volume residue; same run as 1a, so not independent evidence of it; strong class GAP |
| 6 | Failure evidence to operator | **FIXED (unit-tested, not in harness)** | Swallowed `SupervisorError` now surfaces a bounded, path-free `detail` |
| 7 | Restart and recovery | **PASS / GAP** | Control SIGKILLed between requests and again while a real container runs: custody (operation, lease, epoch, grant, hold, events) survives, one container is ever created, the worker's evidence is honest (`unknown`, workspace kept, no receipt claimed). GAP: reconciling a provider effect after a control crash is not proven; recovery ended `quarantine_failed` (container exit 137) |
| 8 | Work runs with no session | **PASS with the grant poller as work source (measured on a temporary merged tree); GAP without a work source** | A supervised worker daemon, started once as a boot unit would be, picks up a submitted job and completes it with an observed receipt, then does the same after control is SIGKILLed and restarted, with the harness starting no worker. Real without a work source: a rejected credential makes the worker CLI exit 3 and the daemon back off by the credential schedule without crash-looping, and its status command reports the degraded state |

## 1. Agent sandbox run

**Network none.** Findings:

- GAP: no public CLI or MCP path runs a network-none agent sandbox on local
  Docker end to end. `tessera agent-sandbox` targets the Kubernetes Agent
  Sandbox; `ordinary-docker-v1` needs a control plane, a human-granted target
  and a pinned guard image that is not present locally.
- GAP (strong class): `scripts/check_local_docker_fixture.py --run` (the closest
  supervised path, a closed harmless program rather than an agent) stops at its
  namespace-proof preflight with `daemon_host_namespace_observer_unavailable`
  and creates no container, so the six strong-class cases (success, failure,
  cancel, deadline, disconnect, stop-cleanup) are unproven on this host. This
  matches [local-docker-namespace-proof.md](local-docker-namespace-proof.md):
  Docker Desktop's LinuxKit boundary cannot supply the independent observer. The
  default zero-effect mode PASSes (`state: disabled, effects: 0`).
- PASS-WEAK: under the explicitly selected weak class all five supervised cases
  run live; see "Weak-attestation class" below.
- Smoke only (**non-platform, provisional**, not the Tessera supervisor path):
  `docker run --network none --read-only --cap-drop ALL --user 65532` against
  the local pinned `python` image failed to connect to an external IP and
  failed DNS resolution.

**Allowlisted egress (ADR 0034).** Landed on `main`, and observed live on
local Docker through the public surface at the last opt-in run
(`real-run / egress-live-run`; raw log not retained). A real
`tessera-worker development-run` executed a task whose approved plan carries
`egress: allowlist-proxy-v1` with the single destination `example.com:80`. The
workload, reaching the host only through the egress proxy, got CONNECT 200 then
HTTP 200 (577 body bytes) from `example.com:80` and CONNECT 403 from
`example.org:80`. The control plane's recorded `execution.egress_journal_recorded`
event agrees: an `allow` row for `example.com:80` and a `deny` row for
`example.org:80`, totals equal to the sums over destinations, `cap_exceeded`
false. PASS needs both the workload's observation and the recorded journal to
agree; the journal is worker-reported, so it is a recorded decision, not
independent proof. PASS additionally requires that the same workload's direct
attempts fail. A direct TCP connect to a public IP literal (no proxy) counts as
blocked only when it ends in `ENETUNREACH`, `EHOSTUNREACH` or a timeout; a refused or
reset connection means packets reached a host and is a FAIL, and any other errno is
undecided (GAP). A direct `getaddrinfo` for a public name must fail (`gaierror`). If
an attempt succeeds the case reports FAIL as an isolation finding. At the last live
run the connect ended `Network is unreachable` (errno 101) and the lookup failed;
that run used a looser classifier (any `OSError` counted as blocked), so the case has
not been validated under the current one. The classifier itself is unit-tested
(`tests/test_platform_validation_harness.py`). Because
the case sends live traffic, it runs only with `TESSERA_PV_LIVE_EGRESS=1`;
without it the egress and staging cases report GAP and no worker run or request is
made. Traffic is two HTTP requests per run to public example
domains; the proxy image is the local `python` image (the adapter mounts the
proxy script), so nothing was pulled or built. The five in-tree synthetic egress
and staging test files also pass (78 passed). The case needs pytest importable
in the interpreter running the harness and reports GAP, not FAIL, when it is not.

## 2. Dataset staging

- PASS (positive path, real runs; `real-run / dataset-staging` and
  `dataset-staged-input-run`): run 1 fetched a small public file through the
  allowlisted egress and declared it as an output. Its manifest was found through
  `GET /projects/{p}/artifact-manifests`, retained (`retain_quarantined`),
  custody-released, staged with `POST /projects/{p}/artifact-snapshots`
  (`expected_snapshot_digest` computed in-process from the control plane's own
  `staged_inventory` and `inventory_digest`, since no preview route exists) and
  released to the worker. `GET /snapshots/{id}/staging` records the source
  manifest and exactly one file with the expected sha256. A second task with no
  network and no egress read `/inputs/page.html` through input delivery and wrote
  its length and sha256; both equal run 1's bytes (577 bytes, same sha256).
- PASS: manual tabular dataset snapshot upload, byte-verified, read back with
  matching digest, wrong pinned size refused with 409, `transfer_authorized`
  false.
- PASS (HTTP denials): the staging route refuses an unknown manifest (404), an
  invalid body (422), unauthenticated calls (401) and an agent credential (403);
  the staging read of an unknown snapshot is 404.

## 3. Lineage and receipts (schema v58)

Graph behaviour, over HTTP (19 cases, plus 3 dataset snapshot cases from item 2):
nodes and edges register with idempotent replay; duplicate digest and duplicate
edge return 409; cycles (409) and self edges (400) are rejected; ancestor and
descendant queries return the expected depths; two exports are byte-identical;
another project cannot read a node (404) and sees an empty export;
unauthenticated writes are 401; an agent credential may read (200) but not write
(403); failed and refused writes left the export digest unchanged.

Chain from three **real** local-Docker worker runs (`receipts-lineage`, through
the public surface, tiny generic `python -c` workloads; no experiment content):

- **Observed receipt (PASS).** The worker posts it automatically after a
  confirmed exit and a sealed output (open hook 1):
  `provider_kind=local_docker_cpu`, `operational_only=true`,
  `provider_success=not_established`, `scientific_validity=not_assessed`,
  `receipt_evidence=observed_provider_execution`, `exit_code` 0, `command_digest`
  equal to sha256 of the canonical argv, `provider_id` the container id; no fake
  receipt row exists for the operation. The grant then reconciles, and a result
  checkpoint and acceptance work over the observed receipt
  (`evidence_label=provider_observed_unassessed`).
- **Derived metric (PASS).** An experiment-admitted grant is run by the real
  worker; the output artifact is linked to the attempt and two numbers are derived
  from its retained JSON by pointer. The values equal the real bytes
  (`derivation=retained-json-number-v1`, `source=derived`,
  `scientific_validity=not_assessed`).
- **Gate verdict (PASS).** Split lock and leakage audits, split binding before
  dispatch, metric records, a platform-computed replicate group over two real
  attempts (n and mean match an independent computation), a version-2 plan, and
  `POST /interaction/decisions` resolved by a human: the platform-computed
  evaluation report passes, a caller float beside the group id is refused with exactly 422 `Invalid request.`
  (the API gives every validation failure that body, so the cause is attributed by
  comparison: the same request without the `value` field is accepted),
  and a human go is approved with `execution_granted=false`. The threshold is
  deliberately vacuous: this shows the machinery, not any science.
- **Lineage join (PASS, human-registered).** Four nodes (dataset, run output,
  derived metric, evaluation) and four edges registered over the real ids;
  ancestor and export queries return the chain.

Receipt verification: control verifies a worker's receipt against its own
records. The receipt's top-level `output_digest` is control's artifact manifest
digest (the worker's local seal digest is kept in the payload as
`sealed_manifest_digest`), and control refuses (409, fixed message) a receipt whose
payload operation id or plan digest, command digest (against the argv in the
approved plan, never the worker's) or output digest does not match its own
records, or when no single matching manifest bound to the same project, task,
grant, lease, epoch and worker exists. The harness posts tampered variants with the
real worker credential and requires each to be refused with that message
(`receipt-bound-to-control-records`). An enrolled worker can still assert exit
code, timestamps and provider identity, which control cannot observe, so a receipt
shows that the control-approved command and output manifest were cited, not that
the process ran.

GAP (platform findings):

- `automatic-lineage-join`: after three real runs, receipts, derived
  measurements and a replicate group, the lineage graph is empty. Nothing emits
  nodes or edges automatically, `GET /experiments/{id}/export` has no lineage
  section (open hook 3), and there is no node kind for a derived measurement,
  replicate group or gate verdict (the check used `other` and `evaluation` with
  caller-chosen digests); no edge links the receipt digest, member measurements
  or decision id into lineage.
- `cross_org_isolation`: a second organization cannot be created over HTTP, so
  only same-organization, cross-project isolation is covered.

Constraints observed on real runs: a task may hold only one unresolved grant, so
replicates of one variant run serially with each grant reconciled first, and a
go requires every linked variant to be admitted and terminally observed. No public
CLI subcommand covers lineage, receipts, snapshots or budgets; these are HTTP
only. MCP coverage of them was not verified.

## 4. Spend controls

PASS (13 cases), all labelled **admission accounting, not a provider cap**:
no budget before authorization (404); a 7-cent cap authorizes and replays
idempotently; a second authorization is refused (409); admitting an attempt
reserves a 5-cent hold (remaining 2); a second admission over the cap is
refused with 409 and admission-scoped rows (attempts, `attempt_admitted`
events, reservations, idempotency keys) are unchanged; the budget revision and exposure are unchanged by the refusal;
a human-asserted 3-cent charge records, replays idempotently and is refused
without credentials (401); `provider_hard_cap_verified` stays `false`.

Limits: no provider hard cap exists, actual cost is always a human assertion,
and the charge draws down the existing hold, so exposure stayed at 5 while the
remaining balance stayed at 2 (observed; the check asserts remaining equals
ceiling minus exposure). No Salad or other paid call was made; the live
Salad harness was not run.

## 5. Stop and cleanup

PASS-WEAK: each of the five weak-class cases records `termination_confirmed` and
`cleanup_confirmed` (exact-ID, non-force removal), and the harness found no
residual container or labelled `/output` volume after the run. These
confirmations come from the same fixture run as item 1a's cases, so item 5 is
not independent evidence of 1a; a separate `stop-cleanup[weak]` row that derived
from that run was therefore removed. GAP (strong class): the six strong-class
cases are blocked before container creation.

Smoke only (non-platform, provisional, does not count): a uniquely named
container was running, stopped with `docker stop`, removed, and no `tessera-pv-`
containers remained. There is no CLI or API stop for ordinary Docker; cancel
there is the human-granted `execution:cancel` action.

## 6. Failure evidence reaching the operator

Fixed. `DevelopmentWorkerService._drive` catches
`SupervisorError` from output collection and returned only
`reason: quarantine_failed`. It returns `detail` as well: the exception class
plus a sanitized message with the worker root and absolute paths replaced by
`<path>`, whitespace collapsed, capped at 200 characters. The reason token and
the unknown-state workspace retention are unchanged, and the worker CLI reports
an escaping `SupervisorError` as a bounded `development worker failed:` line
instead of a traceback. Tests: a plain message, a message with a temporary absolute path, newline
and 400 extra characters, and the CLI stderr diagnostic. The fix was authored by
Claude Sonnet 5.5 (agent `builder`).

Remaining limitation: nothing in `src/tessera_control` consumes the worker
result dict, so the detail reaches the operator through the worker's stdout or stderr,
not a control-plane record.

## 7. Restart and recovery

PASS (10 cases) against a real `tessera serve`: with an admitted attempt, a
5-cent hold and a worker lease, the process was SIGKILLed (while idle between requests, so this proves committed custody survives, not a mid-transaction crash) and restarted on the
same state directory. Event, attempt and held-cent counts were identical; the
pre-kill event rows were an identical prefix afterwards; replaying the project,
admission and lease requests with the same idempotency keys returned identical
results; no second start or duplicate lease occurred (second lease request
409, one lease row).

In-flight (11 further cases, real Docker): a network-none task sleeps in a real
container while control is SIGKILLed and restarted on the same state directory and
port. The operation row, lease, epoch, grant and held cents are preserved (the full
operation row is identical to its state at the moment of the kill, and its custody
identity is identical to before), the event history keeps an identical prefix, an
earlier request replays identically, exactly one `tessera-op-<operation>` container
exists with the same id and start time, and no duplicate receipt or lease appears.
The worker bounded-cancels on a control outage, so it reported `unknown`
(`observation_report_failure`; since ADR 0054 a transient outage ends the run as
`report_pending` and the observation is redelivered later instead); the recovery run reported `unknown`
(`quarantine_failed`, workspace retained) because the killed workload never wrote its
output, and neither claimed success or a receipt. This proves custody and
no-duplicate-start across a control SIGKILL with an honest outcome; it does not prove
a worker that stays alive and reconnects mid-run, since this worker's policy is to
cancel on an outage.

GAP `provider-effect-reconcile-after-crash`: reconciling a provider effect that
completes while control is down is not demonstrated. After the control crash the
worker's outage policy had already cancelled the workload (container exit 137), and
the recovery run ended `unknown`/`quarantine_failed`, so no confirmed exit with a
receipt was reached; the case reports PASS only if a recovery run does reach one. The
Kubernetes single-active restart drill is a live effect and was not run.

## 8. Work runs with no session

A person previously had to start the worker by hand. `tessera-worker-daemon` (see
`docs/worker-deployment.md`) supervises a worker command: it restarts after a clean
exit, backs off exponentially after failures, backs off for at least the credential
minimum after a rejected credential, stops gracefully on SIGTERM and publishes a
private status file. systemd and launchd examples use placeholder paths.

Checks (`no-session`, real processes, local Docker only, no live egress):

- PASS `daemon-credential-backoff`: against a real control that rejects the worker
  credential, the worker CLI exits 3 from a real 401, the daemon enters
  `credential_backoff`, starts are at least the credential minimum apart (3 starts in
  16 s with a 4 s minimum), and SIGTERM stops the daemon with exit 0 and no child left.
- PASS `daemon-status-health`: `status` exits 3 while the daemon waits out a rejected
  credential (alive but degraded), 0 when its child runs, and 1 after it stops.
- `work-runs-with-no-session` and `control-restart-no-session`: need a work source
  (`development-poll`). Without one they report GAP with that exact need. With the grant
  poller (third run above) both PASS: the daemon was started
  once, the harness only submitted jobs and watched control, each job completed with an
  observed run receipt whose digests matched control's records, and the second job
  completed after control was SIGKILLed and restarted on the same port, with the
  daemon's restart count unchanged for the first job.

What still needs a human: installing the unit and credential on the target host, and on macOS
configuring Docker Desktop to start at login, because its Docker socket exists only
while a user session runs it (a launchd daemon at boot has no Docker until then).

## Verification

- Harness: `scripts/platform_validation/run.py`, run with pytest available and the
  repository's `src` and `tests` directories on `PYTHONPATH`. The raw
  run output is not retained in the repository; the counts quoted above are (96 / 5 / 0 / 16 with the local weak opt-in, 95 / 0 / 0 / 21
  with none). No `tessera-*` container, volume or egress network remained after
  either run. The real worker runs leave an exited `tessera-op-<operation>`
  container each, which the harness stops and removes by exact name without force;
  adoption is by the operation ids control recorded for the grant, an exact name or
  an exact `tessera.operation` label.
- Live egress is not exercised in these logs. The last live results for sections 1b
  and 2 predate the stricter direct-connect classifier; reproducing them needs
  `TESSERA_PV_LIVE_EGRESS=1`.
- Harness guards: a failed `docker ps` or `docker volume ls` is a FAIL, never
  `residue=0`; ownership that cannot be established is a GAP; a direct connect passes
  only on `ENETUNREACH`, `EHOSTUNREACH` or a timeout. In weak mode only, an
  `output-volume-precondition` case runs `output_volume_arguments` in the same
  interpreter, directory and environment as the fixture subprocess.
- Several checks are partly white-box: they use the public HTTP surface plus
  read-only reads of the state database and in-process digest computation from
  `tessera_control`. The evidence strings say so where it applies.
- Provenance: the harness is owner-operated automation. It sends
  `X-Tessera-Automation: platform-validation` only on decision routes (task
  approval, grant issue, attempt admission, snapshot release, budget authority and
  the resolution of an interaction decision). The label is client-asserted and not
  server-verified: any holder of the owner credential can send any label. The
  `decision-provenance-recorded` case reads every stored decision event and requires
  `decision_class: owner_automation_labelled`, `automation_label:
  platform-validation`, `credential_kind: local_owner` and `automation: true`, an
  approval or grant decision besides the budget authorization, and no label on a
  non-decision event (`owner_automation_unverified` is reserved for the exact label
  `desktop-owner`).
- Full suite, run once before merge, as four concurrent disjoint shards
  (`dev/pytest_parallel.py --jobs 4`, with the repository's `src` and `tests`
  directories on `PYTHONPATH`): 1456, 2120, 1618 and 2030 passed (7224), and one test
  failed, `tests/test_artifact_lineage.py::test_http_routes`. No change under test
  touches the lineage code or its tests, and the file passes when run on its own (three
  of three). The probable cause is in the listing: nodes are ordered by
  `created_at, id` with random ids, so two nodes created in the same timestamp tick come
  back in random order while the test assumes creation order. That is an intermittent
  defect in the test or the ordering, not addressed here. The run took about 35 minutes
  because the shards slow each other down when run together. Between full runs only focused
  tests are run (`tessera dev focused-tests`, ADR 0117); the full suite is a once-per-change pre-merge
  run (`make test-parallel`).
- The focused worker, daemon, refusal and harness tests (181) and the test-tooling tests
  (42: the changed-test selector and the parallel runner) pass; with the grant poller included, 163 worker-side tests
  pass, including the poller's own tests.
- Ruff check and format are clean on the changed paths; `dev/check_docs.py` is clean.

## Weak-attestation class for items 1a and 5 (ADR 0035, accepted)

[ADR 0035](decisions/0035-docker-desktop-network-attestation.md) adds an
explicitly selected class, `engine_declared_workload_observed`
(`--attestation-class`, default unchanged), recorded on the receipt and never
relabelled as `daemon_host_independent`. It is a narrow ADR 0033 exception. A
result under it is reported only as **PASS-WEAK**, and network-isolation claims
stay GAP under the strong class. It is tested offline
(`tests/test_local_docker_attestation.py`, `tests/test_local_docker_interfaces.py`;
the existing namespace, fixture, limits and guardian tests pass unmodified).

Root cause of the exit 78 formerly seen: Docker Desktop's kernel creates fallback
tunnel devices (`tunl0`, `gre0`, `gretap0`, `erspan0`, `ip_vti0`, `ip6_vti0`,
`sit0`, `ip6tnl0`, `ip6gre0`) in every namespace. They are DOWN, with no address
and no route, so isolation held; the check `names == ['lo']` was stricter than
the property it witnessed. `/sys/class/net` also lists a regular file,
`bonding_masters`. For the weak class only, the check accepts a reviewed finite
set of fallback devices that are DOWN, address-less and route-less, and that one
sysfs file, and rejects anything else (unknown name, UP, addressed, routed,
duplicate, wrong file type). The strong predicate, program and v1 digests are
unchanged. A bounded, path-free diagnostic (interface names, flags, counts,
nameserver line count only) is retained on failure.

Result: the network proof reaches `workload_observed` and **all five cases are
PASS-WEAK** (success `completed`, failure `failed`, cancel `cancelled`, deadline
and broker-loss `unknown` by the fixture's contract), each with termination and
exact-ID non-force cleanup confirmed and no container or volume left. `success`
and `failure` had been blocked by an independent output-capture bug (`docker cp`
of the `/output` tmpfs returned an empty directory on this engine); `main` now
backs `/output` with a labelled tmpfs-typed local volume and the cases pass.
PASS-WEAK is engine-declared and workload-observed, not independent proof: it
proves nothing a compromised image or engine could not forge. The strong class
also rejects the fallback devices and may therefore reject native Linux hosts
that create them; this is flagged, not changed.

## Intermittent weak-class failure: cause and fix

The weak `failure` case could end `unknown` (fail-closed), and the fixture stops
after an unproven case, so PASS-WEAK fell to 0. Repeated runs on an otherwise idle
host passed, so the failure was reproduced by running the fixture while CPU-bound
processes outnumbered the cores, with the evidence directories kept. Every loaded run failed closed after the stop was confirmed:
the journal shows `stop_confirmed` then `stop_unknown` (not `stop_cleanup`), then
`recovery_unknown`, with `cleanup_confirmed=false` although no container or volume
remained. In one run the network-proof step also timed out.

Cause: every Docker call in the fixture's engine wrapper was bound to 2 seconds,
and a timeout raises an unknown reply, which is correct. The post-removal absence
check, however, listed every container on the host. That call grows with the
number of stopped containers a shared host accumulates (a few hundred containers
already cost a large fraction of the bound when idle), so under load a slow reply
failed a confirmation that had in fact succeeded.

Fix (generic, fail-closed preserved): absence is an exact-ID filtered query that
accepts only a successful empty reply (any ID, matching or not, is invalid
evidence), and the post-termination cleanup calls (the readback after a kill or
exit, `rm`, the absence query, output-volume removal) have their own 10 second
bound. Every other call, including the pre-termination inspect, `unpause` and
`kill`, keeps 2 seconds, so runtime, lease and pre-start safety probes are
unchanged, and a timeout is still unknown, never absence. Offline tests
(`tests/test_local_docker_timing.py`) cover the exact-ID query, the invalid
evidence cases and the per-call bounds. Under the same kind of load the full
weak-class run passed with cleanup confirmed in 4 of 4 runs, where unfixed runs
had failed closed. Still bound to 2 s and load-sensitive: the pre-start network
proof (peer inventory, peer inspects, observer exec) and the in-window `pause`,
`cp` and `logs`; these are safety probes and are not widened without a decision.
Load generation for such a reproduction must be bounded: an over-large load
slows every job on a shared host.

## Remaining GAPs and what each needs

| GAP | Needs |
|---|---|
| Six strong-class Docker cases | A Linux daemon host with an independent namespace observer (ADR 0035 strong class); not available on Docker Desktop |
| Public path for a network-none agent sandbox on local Docker | A product decision and adapter: `tessera agent-sandbox` targets Kubernetes, and the development worker is the path exercised here |
| `cross_org_isolation` | An organization-creation surface; the local product has one organization created at `init` |
| `egress-live-run`, `dataset-staging`, `dataset-staged-input-run`, `docker-residue` (real-run) | Explicit authorization to send live requests (`TESSERA_PV_LIVE_EGRESS=1`); previously PASS when enabled |
| `provider-effect-reconcile-after-crash` | A worker path that survives a control outage without cancelling, or a recovery path that reconciles a workload completed while control was down, plus a workload that completes in that window |
| `work-runs-with-no-session`, `control-restart-no-session` | A work source (`development-poll`); both PASS with the grant poller |
| `automatic-lineage-join` | A design (ADR) and implementation: emission from operation, attempt, artifact and derived measurement into nodes and edges, node kinds for derived measurements, replicate groups and gate verdicts, and the experiment export's lineage section (open hook 3) |

Other known limits: the worker's `quarantine_failed` path (a collect failure such as
a permanent upload refusal) re-seals or re-uploads on each operator restart with no
terminal marker; it does not loop automatically. The pre-start network-proof probes
and the in-window `pause`, `cp` and `logs` remain bound to 2 seconds and are
load-sensitive; they are safety probes and are not widened without a decision.

## Remaining work

Decide whether the strong class should accept reviewed fallback devices; run the
namespace-proof fixture on a Linux host for the strong class; and design lineage
emission.
