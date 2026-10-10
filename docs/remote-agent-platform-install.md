# Governed remote-agent installation and operator path

Status: operator preparation guide; **not** a claim that the integrated remote
agent platform is complete or live-accepted. See the [design contract](../DESIGN.md#53-governed-agent-identity-reasoning-and-peer-transports),
[implementation journal](remote-agent-implementation-journal.md), and
[implementation backlog crosswalk](../IMPLEMENTATION_TASKS.md).

This guide uses ordinary Tessera package/service entrypoints. It does not require
private developer receipt files, temporary helper launchers, or special OAuth
runner images. Local Docker Desktop is laptop-local, not an independent host.
Do not deploy while implementation lanes are writing the shared tree; wait for
their convergence and the operator's separate target/credential authorization.
For the typed grant contract see
[`agent-authority-interface.md`](agent-authority-interface.md); it is an interface
specification, not a claim that every route is implemented.

## Supported installation facts

- Python package requires Python 3.11 or newer. The installed command names are
  declared in `pyproject.toml`: `tessera`, `tessera-worker`, `tessera-mcp`, and
  `tessera-reasoning`.
- The optional `mcp` extra pins the official Python MCP SDK to **1.26.0**.
- The optional `a2a` extra pins `a2a-sdk[http-server]==1.2.1`; the
  `tessera-a2a-peer` console entrypoint is registered in `pyproject.toml` and the
  dependency is present in `uv.lock`. The A2A contract selects protocol 1.0.0
  with JSON-RPC. A fresh wheel install is not verified here.
- `tessera-mcp` uses session-based MCP protocol **2025-11-25** over **stdio**.
  It is a child process launched beside an MCP-capable agent/client; it is not a
  network MCP listener or the stateless 2026-07-28 protocol.
- The stdio process calls the configured Tessera control origin using the
  enrolled agent's credential file. Remote control requires verified HTTPS; an
  optional public CA bundle is configured by file reference. Do not disable TLS.
- Use the existing [separate-service installation runbook](installation.md) for
  building/installing the wheel, initializing/serving a control process,
  operator-owned worker setup, and recovery. That runbook's owner header is for
  the human operator only and must never be passed to an agent. Desktop uses a
  separate Node/Electron package; its documented Node minimum is **22.12.0**.
- Linux systemd unit templates, the `LoadCredential` startup mapper, and an
  operator lifecycle guide are in
  [`deploy/remote-agent-platform`](../deploy/remote-agent-platform/README.md).
  The startup mapper is `src/tessera_agent/service_startup.py`, with focused
  coverage in `tests/test_remote_agent_services.py`. The owner reports 31 service
  profile tests plus 26 runtime regressions (57 total) passing, including the
  corrected credential mapper. These are local tests; no Linux/systemd service
  run or fresh wheel installation occurred. The templates do not enroll agents,
  start OpenCode, or authorize inference by themselves.

Reproduce the repository development installation and inspect installed entry
points without starting a provider or an agent run:

```sh
uv sync --locked --extra mcp --extra a2a
.venv/bin/tessera --help
.venv/bin/tessera-mcp --help
.venv/bin/tessera-reasoning --help
.venv/bin/tessera-a2a-peer --help
```

For a separately installed environment, follow `installation.md`'s wheel
instructions and install the reviewed artifact with `[mcp,a2a]` extras. Do not
guess or substitute a wheel version. Desktop installation remains separate.

## Operator and agent topology

1. An operator selects and administers one authoritative control service and
   storage home using the existing [installation guide](installation.md). A
   remote worker/control host must be independently reachable and supervised;
   a process on a laptop or its Docker Desktop cluster does not meet laptop-off
   acceptance.
2. A human operator enrolls a **distinct agent identity** for the intended
   project and destination using an implemented, documented owner workflow. The
   resulting agent credential is kept in a private, owner-only regular file
   readable by the account running the stdio process. Never use an owner token,
   worker token, provider secret, or another agent's credential here. Never put
   credential contents in command arguments, environment variables, client
   configuration, prompts, logs, or this repository.
3. Configure the MCP-capable agent/client to launch the installed
   `tessera-mcp` executable using its supported local **stdio** process
   configuration. Client configuration formats differ; use that client's
   documented `command`, arguments and environment fields, not a guessed
   universal JSON schema. Supply only these references/identifiers to its child
   environment:

   | Environment key | Value |
   |---|---|
   | `TESSERA_CONTROL_URL` | Control service origin; remote origins must use HTTPS |
   | `TESSERA_AGENT_TOKEN_FILE` | Private file path for this enrolled agent credential |
   | `TESSERA_PROJECT_ID` | Project this credential is scoped to |
   | `TESSERA_AGENT_DESTINATION` | Enrolled destination bound to this connection |
   | `TESSERA_CONTROL_CA_FILE` | Optional path to the public CA trust bundle |

   The adapter reopens the credential file for each request and checks that it
   is a regular, non-symlink, current-user-owned file with no group/other
   permissions. It never needs the token as an environment value. The control
   endpoint must authorize the credential for the selected project and
   destination; changing a client-side identifier does not grant access.
4. For an A2A reference peer, the installed entrypoint is
   `tessera-a2a-peer --config /private/peer.json --port 8771`. Keep its durable
   database/config private; the peer credential-to-identity map must be
   operator-bound and its control credential must be an enrolled agent
   credential. See [`a2a-peer.md`](a2a-peer.md) for the exact closed profile,
   trust rules, supported protocol subset and TLS boundary. A reference peer is
   not by itself a reasoning critic or notebook writer.
5. Once authority backend and research routes are integrated on the selected
   control service, connect the client and begin with non-mutating identity and
   disclosed-context reads. Verify returned agent identity, scope, destination,
   and expiry before proposing work. A tool's presence in the MCP catalog is not
   proof that its backend route is deployed or authorized.
6. An agent may propose a project/study/experiment, request authority, or submit
   attributed observations/review requests. These are not canonical human
   decisions. A separately authenticated human reviews and issues any bounded
   grant. Only then may the agent consume that grant for its exact bound
   operations. Preserve each idempotency key with its exact request across
   reconnect; an uncertain response is a reconciliation case, not permission to
   create a replacement operation or broaden the grant.
7. Review the redacted notebook/export with its completeness disclosure. Missing,
   denied, failed, or unknown provenance must remain visible. A successful
   process or agent assertion is not artifact acceptance, provider settlement,
   human review, or scientific validity.

## Current implementation and gates

The installed stdio MCP server and SDK dependency are present. Its registered
tools include scoped status/context/proposal reads, proposal submission, authority
request/consumption, research proposal, observation, review request, and notebook
reads/export. The backend owner now reports schema 51 authority/research workflow
and provenance integration, **63 focused tests**, and fresh plus explicitly
migrated temporary control readiness returning HTTP 200. The explicit v50-to-v51
migration does not infer new scopes for old credentials. Broader combined
regressions timed out without a reported failing test; they are incomplete, not
passing evidence.

An earlier standalone MCP rerun stopped at HTTP 503 `schema_mismatch` while schema
integration was active; that historical failure is retained below. The later
schema-51 readiness-200 check is separate temporary-state evidence, not proof the
MCP workflow passed. The owner now reports a successful, bounded real
SDK/stdio/control workflow described below. Its grant/task resources and setup
were synthetic; it did not run a worker or external executor. Human enrollment
and grant issue used a separately authenticated human HTTP/CLI path, not a
user-provided owner token or an agent decision.

Implementation paths now include `src/tessera_mcp/server.py` and
`src/tessera_mcp/mcp_workflow.py`; the workflow registers **15 typed tools**.
`docs/mcp-full-workflow.md` owns the precise schemas, endpoint requirements and
opt-in verification contract; `scripts/check_mcp_control.py`,
`scripts/check_mcp_full_workflow.py`, and new
`scripts/check_mcp_workflow_local.py` cover distinct boundaries. The MCP test
module is `tests/test_mcp_workflow.py`; the owner reports **46 targeted tests and
scoped checks passed** for the current change. The earlier 503 `schema_mismatch`
run remains a failed attempt; a later schema-51 backend check was ready, and the
final standalone control harness passed.

The bounded real MCP protocol/control scenario made **29 SDK/stdio/control calls**
successfully: proposals, separate authority request/issue and admitted
admit/dispatch/status/cancel calls, observation/review, dual-cursor notebook/export,
reconnect replay, denial, revoke and expiry. It exercised the real SDK subprocess
and control HTTP, not an in-process transport substitute. A dual-cursor notebook
pagination bug was fixed in this pass. No worker/executor or Kubernetes cluster
consumed the dispatched work; execution resources were synthetic package/queue
fixtures, and there was no inference or paid/provider call. Fixture processes were
cleaned up. Treat this as a passed protocol/control-surface boundary, not ordinary
installed-wheel acceptance, remote experiment execution, A2A critic integration,
or the integrated researcher journey.

The provenance implementation now includes
`src/tessera_control/agent_provenance.py` and
`src/tessera_control/agent_provenance_models.py`, tested in
`tests/test_agent_provenance.py`; the human and
agent-scoped projection helpers are specified in
[`agent-provenance-interface.md`](agent-provenance-interface.md). The provenance
module owner now reports **44 SQLite module tests**; these use local records and
do not prove live runtime delivery. The backend owner reports schema 51
registration, explicit `provenance:append` enrollment (maximum nine actions, no
inferred old scopes), and a bounded collector router registered on the app. Four
focused HTTP integration tests (**8.80s**) passed for grant-bound
append/replay/conflict, notebook/export reads and scope/destination/project/
subject/revocation denials. A fresh database and explicit schema-50 migration to
51 both reported readiness 200. The backend reports same-snapshot human exports
in its focused 63-test suite. Runtime and A2A now report durable outbox delivery
through the shared client; see the dated journal checkpoint below. Delivery tests
used local/in-process collector transport (and local SQLite), not a TCP
ordinary-service boundary. Joined export and actual runtime reasoning remain
separate acceptance gates.

The supervised reasoning implementation is
`src/tessera_agent/remote_reasoning_runtime.py` with its durable
`remote_reasoning_store.py`, `tests/test_remote_reasoning_runtime.py`, and opt-in
live checker `scripts/check_remote_reasoning_live.py`. The owner reports
**26 synthetic tests** and scoped checks/module-help passing. The ordinary installed
`tessera-reasoning` console entrypoint is now registered in `pyproject.toml`;
fresh wheel build/install and running that installed command are not yet verified.
The Python-module invocation and `tessera-reasoning` entrypoint usage are documented
in [`remote-reasoning-runtime.md`](remote-reasoning-runtime.md). The
deterministic/provider-disabled coordinator is not a reasoning substitute.
Runtime and operator commands are not permission to contact a model.

The service profile under `deploy/remote-agent-platform/` includes systemd units
for control, reasoning and one A2A peer. Its fixed `LoadCredential` mapper is
`src/tessera_agent/service_startup.py`; the owner reports 31 service-profile and
26 runtime tests (**57 total**) passing in `tests/test_remote_agent_services.py`.
No Linux/systemd host was available and no fresh wheel was installed. The bundle
is a reviewed, loopback-only operator template; it does not mean any unit is
installed/started, does not create a dedicated OpenCode provider profile, and
does not authorize inference. See the bundle README for admission-marker,
manual-start and stop/drain semantics.

The runtime observes token usage and interrupts after an observed cap; this is
not a hard pre-token financial limit and one in-flight provider step may exceed
it. An interrupt acknowledgement is not proof of provider termination. Do not
claim cost hard-bounding, termination, or an experiment stop from those states.
The installed OpenCode V2 `2.0.18-subscription.20260929` CLI is guarded by the
runtime, and its harness calls use HTTP Basic authentication (the Tessera control
API continues to use its scoped bearer credential). A dedicated process and
workspace are possible, but the checked installation shares the owner's account
database and no dedicated research profile is configured. Do not connect the
general coding service or treat process separation as account/provider isolation.
An isolated local test agent credential can be minted through the separately
authorized human setup API; this is implementation/setup work, not a credential
the user must supply again. **No research-profile admission or inference has
occurred.** The exact proposed profile is a separately guarded process/workspace
on the shared owner database, without copying OAuth material; deny by default with
only the approved MCP tool whitelist; operator-approved model; at most 3 steps,
120 seconds, and 8192 observed tokens; and included ChatGPT OAuth only, with no
API-key or paid-credit fallback. The token cap is an observed-usage fence, not a
hard pre-token ceiling. See [`remote-reasoning-runtime.md`](remote-reasoning-runtime.md).
An authorized independent host/laptop-off target is a separate user decision; it
is not a prerequisite for bounded local reasoning.

## A2A peer implementation and bounded evidence

The reference peer is implemented in `src/tessera_control/a2a_peer.py` and
`src/tessera_control/a2a_store.py`; its ordinary module/console launch is
`python -m tessera_control.a2a_peer --config /private/peer.json --port 8771` or
`tessera-a2a-peer --config /private/peer.json --port 8771` after installation
with the `a2a` extra. See [`a2a-peer.md`](a2a-peer.md) for the closed identity,
credential, task, and bounded-delegation profile. It supports the tested A2A
1.0.0 JSON-RPC subset; it is not a universal conformance claim. Streaming, push,
extended cards, and ListTasks are unsupported.

The A2A owner reports `tests/test_a2a_peer.py` and `tests/test_a2a_store.py`
coverage: 10 SDK/store tests plus a body-limit test; the default suite reported
4 passed with one optional-SDK skip, and scoped Ruff checks passed. The local fixture
used two independent real peer processes, two SDK clients, and real control HTTP
for discovery, exchange, restart/replay, cancel/late-result, authentication,
cross-peer/stale-epoch denial, ancestor revocation, and expiry replay denial.
Provisioning records and worker setup were synthetic; this is real local protocol
coverage, not autonomous agent reasoning, independent-host acceptance, or a
joined notebook export. The A2A peer returns caller result references, not a
verified critic conclusion. Its task/message/cancel events still need the
provenance lane's bounded HTTP collector and authorized notebook integration.
The A2A owner reports `src/tessera_agent/a2a_reasoning_bridge.py` with an optional
terminal `--deliver-provenance` path through the shared client and **34 focused
tests**. Local HTTP/SQLite checks exercised joined notebook append/replay, matching
receipts, atomic acknowledgement and denied/unknown outcomes remaining pending.
The runtime reports **58 targeted tests** for its shared client and durable outbox.
These collector/outbox tests exercised real collector routes and SQLite through an
in-process synthetic transport, not TCP to an ordinary service and not real
reasoning. They establish local delivery implementation behavior only; do not call
peer exchange an autonomous critic or use it to verify the referenced result.
The optional runtime `httpx` dependency is now declared and locked.

Current source metadata and lock contain the `a2a` extra, pinned SDK, and
`tessera-a2a-peer` entrypoint. A fresh installed platform wheel/image and
`pip check` were exercised; two independent installed A2A peers passed the bounded
protocol exchange recorded in the journey receipt. This does not itself verify
the dedicated console entrypoint from a clean wheel invocation; reconcile the
older packaging-pending note in `a2a-peer.md`. The integrated
planner-through-MCP / critic-through-A2A research journey, real reasoning,
restart/revocation/expiry with retained output, and complete joined export remain
open.

The opt-in journey orchestrator is `dev/remote_agent_platform_live.py`, with
`tests/test_remote_agent_platform_live.py` and
[`remote-agent-platform-journey.md`](remote-agent-platform-journey.md). Its owner
reports **43 focused tests** passed in 5.55s, including automated human enrollment,
schema adaptation, expected self-enrollment 403, and a read-only, UID-fenced
restart-generation baseline probe. The baseline passed but no service restart was
performed, so this is not restart recovery proof; the 43-test checkpoint itself
did not run an integrated live journey. Later installed partial/blocked attempts
and their exact handoff are recorded in
[`remote-agent-platform-journey.md`](remote-agent-platform-journey.md). The earlier
read-only preflight found an expired TLS route with no listener
and an ordinary worker unready with 36 restarts. A later authenticated read found
the existing worker credential expired at 11:54:43Z: the owner's GET and worker
status returned 401, `ControlChannelError` was logged, and the process had restarted
39 times. An expired credential cannot be renewed; preserve its journal/history
and owner-enroll a fresh identity rather than extending or reusing it. Those were
the earlier preflight conditions: its inspected wheel lacked newly added modules,
planner references were placeholders, and dedicated reasoning references were
absent. Subsequent distinct two-hour planner/critic/worker identities were
enrolled, and a fresh installed wheel/image was built. The older grant was not
reused as new authority. The later two-hour credentials expired at 21:14 UTC;
the current queued-dispatch handoff must continue through the original worker and
control journal, not by replacing identities or altering old bindings. Dedicated
approved reasoning OAuth profile/references are still absent.

The 43-test harness update reports automated private human-API planner/critic
enrollment, optional fresh-target worker registration, status-only child
delegation and peer configuration. It also covers the expected 403 for agent
self-enrollment. No service was restarted, worker identity replaced, or Job
mutated in that checkpoint. Fresh-worker identity/journal repair is planned and
is an implementation step, not a permanent prohibition.

The frozen Python source gate completed **3454 passed, 0 failed, 1 skipped** on
its exact unchanged digest; per-shard counts are in the journal. That baseline
predates later maintenance and provenance/outbox changes. No full gate has run on
the current source. The targeted no-package recovery repair passed focused tests
and was installed, but did not handle queued-dispatch recovery.

The earlier queued-expiry proof attempt remains preserved: Job LIST returned 403,
then the expired original bearer returned 401; the worker was stopped and no
attestation/release was submitted. A later separately authorized namespace-only
Job/Pod GET/LIST role and five-minute original-human-issuer authorization allowed
proof and reconciliation (HTTP 201 issue, 200 release, identical replay). The
maintenance grant minted no bearer/worker/execute/renew/provider permission; a
separate temporary service-account token was used only for approved namespace
Job/Pod reads. The original worker was not revived and provider termination was not inferred; see
[`queued-dispatch-maintenance.md`](queued-dispatch-maintenance.md).

Distinct installed run 5 passed all **16 configured journey stages** and created
a real Job; its **36-byte** output hash and authorized readback matched. This is
not operational or scientific success: lease cancellation made the output late
and quarantined, and the supported resolution was `cancelled`. The eight-page
notebook export remains `partial_page`, metadata-only, and omits the output
manifest ID; critic outbox delivery is unverified. MCP observation/review passed,
and a separate pre-submission cancel/reconnect test passed; it is not a running-Job
termination test. Details are in
[`remote-agent-platform-journey.md`](remote-agent-platform-journey.md).

Ordinary worker renewal is already implemented: the installed service sends
durable-key `POST /worker/leases/renew` actions every 10 seconds, with exact-epoch
and hard-expiry checks. The focused selected lifecycle tests reported 24 passed,
38 deselected and two dependency warnings in 115.45 seconds; the outer shell timed
out after the pytest summary, so no shell exit code was observed. See
[`ordinary-worker-renewal-diagnosis.md`](ordinary-worker-renewal-diagnosis.md).
Missing renewal is not a source implementation gap.

The earlier schema-51 collector checkpoint had two unresolved seams: output
manifest/notebook correlation and A2A message/result/cancel outbox acknowledgement.
The newer schema-52 installed receipt supersedes those source-gap reports: it
contains the actual output manifest/artifact/attempt/operation/Job-UID join and both
installed peer drains were fully acknowledged in that bounded installed scenario.
The earlier “no known source gap” status was specific to those schema-52 seams and
is withdrawn as a whole-platform statement by later schema-53/54 source findings
and missing generated-runtime integrations. It does not close root integrated
acceptance or update the installed schema-52 receipt.

The schema-52 read-only worker snapshot adds partial causal data for prior run 5:
zero renewal actions, terminal worker exit at 21:53:20.499345Z, lease expiry at
21:53:32.979713Z, manifest record at 21:53:34.220823Z, and a later human cancel at
21:55:11.242538Z. Thus manifest recording was after lease expiry and the later
human cancel did not cause the initial late flag. The exact cancellation reason
remains unrecorded; do not infer it. The new fresh normal run had `late=false` and
supported `succeeded` resolution, confirming the ordinary supervised sequence
without a private keeper. The historical 3454-pass source baseline does not cover
schema-52 changes. At this older checkpoint, the current full gate was blocked
before results at coding-agent admission. A later candidate's exact-source gate is
summarized in the [release evidence note](release-evidence-intent9.md), but it does
not establish a new full integrated remote-agent journey. Do not relabel this
historical receipt.

### Latest full integrated remote-agent platform receipt — 2026-10-05

This historical installed platform journey is indexed as local evidence ID
`REMOTE-AGENT-PLATFORM-2026-10-04`; it records schema 52, image
`sha256:ff90ef05b7ba771c2ee400b3f475e29eeda48cc84a74fe2b61583eb5a2f2b950`
and wheel SHA256 `d982601e437a0858af1a3572d9e0cd33dd763e40c387c609cd588babd17fd33f`.
An explicitly owned namespace/project kept the old six-Job bench and its quota
unchanged; run-5 cancelled/quarantined and run-8 quota-denied/unknown histories
remain preserved. The normal attempt produced a real on-time Job/output, resolved
through supported control `succeeded`, and joined its manifest, artifact, attempt,
operation and validated Job UID in the final notebook. Human disposition still
retains quarantine with custody released; scientific validity is not assessed.
Each of two installed reference peers drained four causally linked metadata
records. All 16 configured journey assertions passed, while the receipt correctly
keeps reasoning/independent-host/laptop-off/integrated-root-acceptance flags false.
This is bounded local Docker acceptance, not autonomous critique or scientific
validation.

Later schema-53/54 source checkpoints add a typed generated-public-output registry,
public-capture provenance/outbox and a bounded ordinary-Docker worker HTTP/service/
CLI adapter; see the [generated-output contract](generated-public-output-interface.md),
[public-capture interface](public-capture-interface.md), and
[ordinary-Docker binding](ordinary-docker-http-binding.md). Their focused
fixture/temp-HTTP results are not an installed schema-54 wheel, a live V2 runtime
capture, an approved guard-image build, real generated reasoning, or garage-host
acceptance. The current-source full gate remains uncollected; refer to the
[implementation journal](remote-agent-implementation-journal.md) for the current
acceptance matrix and blockers.

**Current source note (2026-10-07):** schema 55 now adds runtime-binding records,
with separate local backend/API/native-owner checkpoints. This does not replace the
schema-52 installed receipt below, establish production owner enrollment, or prove
the current package is installed. No schema-55 candidate or current full-source gate
is available; see the [release checklist](release-checklist.md) and
[runtime operator setup boundary](runtime-operator-setup.md).

### Revision-3 default research factory and attestation boundary

The latest source checkpoint now wires the default `ProductionResearchFactory` to
configured HTTP, MCP-stdio, A2A, context, capture, registry, human-record, input and
experiment ports. Its `V2AttestedHarnessObserver` uses fixed `GuardTrust` references
and checks issuer trust, fresh nonce/30-second age, revocation, closed fields and
pinned config/catalog/skill/account/hook observations. The default factory is not a
fixture-port stub and has no unconditional `exit 78`: absent/invalid required proof
fails ordinary admission before effects. The consumer ports are documented in
[`research-consumer.md`](research-consumer.md).
Separate observer-case receipts report 26 synthetic cases plus 3 age-boundary cases;
they are component evidence, not another aggregate and are not added to the 171-case
count.

The scoped 171-case targeted aggregate reported passed, three warnings, no skips,
exit 0, with Ruff passing on source digest
`a3eda2bd89d181b45f8d2140457d4b4c9a8da6e1a834b83316b8cbb0da333e01`. This
supersedes the 133-affected targeted gate after observer changes, **not** the
historical 3,454-pass/1-skip full Python gate. The 171 tests used temporary
HTTP/SQLite, MCP stdio and two independent A2A SDK server instances; model,
admission, guard measurements and peer identity/control facts were synthetic. A
separate-context source digest `97cf201898027afdbacd2b43575a66d6652f5bc5b6997e1aa064393bc7bf638a`
has its own 27-focused-test/exit-0 receipt. A separate 130-case printed summary had
unknown process exit and is not included in either count. No linked receipt path was
provided with the 171 source digest.

This verifies the observer's handling of issuer claims, not the truth of their
measurements. Intent-6 Decision 0029 approved the evidence-class semantics: strict
v1 remains the default; v2 requires explicit human opt-in; provider entitlement and
billing may be unknown only under the reviewed policy, while unknown required local
checks deny. V7 local binding executes the owner/guard/quota/API/Store/native source
path using synthetic upstream identities/resources; production enrollment and
installed private resource handles remain unsupplied. The
`/tessera/runtime-guard/v1/observe` route is a Tessera integration contract, not an
existing OpenCode API. No further generic semantics decision or admission lowering
is implied. Do not copy OAuth/provider credentials, inspect keychain, or use API-key
fallback.

The Docker profile source and configured factory wiring exist, but no guard image
was built/enrolled and no env/reference file or credential was read. The denied
`deploy/remote-agent-platform/docker/.env.references.example` operation remains
untouched and is not a global edit prohibition; the configured `*.env.*` rule was
only a plausible, unconfirmed match. The separate garage inventory safety refusal
also remains unresolved; garage workloads/auth references are uninspected, not
known absent. Schema 52 is still the latest installed journey; source is schema 55,
and the current full-source gate is unrun/uncollected. No live provider request is
claimed.

The repository's
[`remote-kubernetes-oauth-reconnect-e2e.md`](remote-kubernetes-oauth-reconnect-e2e.md)
is a separate opt-in fixture requiring a named remote Kubernetes namespace,
pre-existing runner image/credential bridge, and explicit one-call provider
authorization. It is not a prerequisite for installing the ordinary stdio MCP
client, and this runbook does not invoke it. No such live credential or
independent-host result is asserted here. The private off-host target is authorized
by the current intent, but its inventory/runtime authentication remain
uninspected in this checkpoint.

### Reconciliation after implementation lanes converge

- [ ] Build/install the converged package and verify the registered
  `tessera-reasoning` command is present and usable, along with protocol versions,
  credential enrollment/revocation workflow, and route availability.
- [ ] Build and install the actual distribution artifact with `[mcp,a2a]` in a fresh
  environment; verify the installed help and stdio handshake, not only the
  source-tree command.
- [ ] Exercise all typed MCP tools against the ordinary installed control service
  under an agent-scoped credential; verify denied human-only decisions and
  cross-project/destination access.
- [ ] Reconcile `docs/a2a-peer.md`'s packaging-pending note with current
  `pyproject.toml`/`uv.lock`; install the actual `[a2a]` wheel and verify the peer
  entrypoint and two-peer behavior from that artifact.
- [ ] Validate the documented reasoning module/service command in a fresh
  installed artifact and record the secure credential-reference/service setup;
  real reasoning needs separately authorized provider runtime credentials/budget.
- [ ] Resolve a truthful guard producer and dedicated origin/attestation admission;
  do not treat the source observer verifier or fixture-generated guard facts as
  proof of real policy/catalog/skill/identity/OAuth/hook measurements.
- [ ] Build/install the guarded profile only after trustworthy evidence admission;
  exercise the ordinary service, real generated planner/critic, human review,
  experiment/notebook, active-run restart and real running-job cancellation.
- [ ] Collect the current full-source gate through its existing authorized child;
  the 171-case targeted source aggregate does not replace it or the historical
  3,454-pass/1-skip baseline.
- [ ] Before candidate freeze, reconcile README, design, installation guide,
  interface docs and backlog against the exact built artifact. The 2026-10-07 docs
  batch distinguishes schema-55 source from the latest schema-52 installed receipt;
  it does not claim a final candidate or complete docs-release reconciliation. Run
  scoped link/diff checks and applicable artifact gates, and distinguish local
  Docker from independent-host/laptop-off acceptance.

### Desktop integration gate and remaining UI evidence

The desktop leaf reports the 41 desktop root/test files at SHA-256
`828be9665891ccad6bc6717f4967347d78377fcf44419d76b3e8cf4460ce2a33` unchanged
during its final gate. `npm test --prefix desktop` reported **205 passed** with no
failures/skips/cancellations in 3.618s; `npm run check` and scoped diff checks
passed. The stale Save-stage assertion in
`desktop/test/journey-bridge.test.mjs` was corrected for the staged Save helper.
The npm-pack/Electron hidden-window smoke reported sandbox and context isolation
enabled, Node integration disabled, no renderer errors, null status, and clean
exit 0. There is no separate desktop build script. The test host had Node
**22.2.0**, below the documented **22.12.0** minimum; that caveat does not erase
the observed checks or prove a supported installation.

Actual OS picker use and the connected native workflow are still unverified:
agent credential → Open picker → released artifact preview → Save picker → verify
saved bytes/hash → reconnect. See [desktop workflow notes](../desktop/README.md);
the opt-in Electron fixture's file-selection fallback is not actual-picker
acceptance.
