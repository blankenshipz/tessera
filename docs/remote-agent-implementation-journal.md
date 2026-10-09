# Governed remote-agent platform — implementation journal

Status: active integration record; implementation and acceptance remain **not
verified by this journal**. Updated: 2026-10-08.

Sanitized local record IDs in this journal identify retained local records; they
are not links to bundled artifacts or standalone public verification.

## Outcome and evidence boundary

Deliver an integrated governed remote-agent research platform in the current
Tessera repository, ordinary installed services, and authorized local Docker
Desktop integration. The six review principles are tracked as authority,
durable provenance, supervised real reasoning, full governed MCP, genuine A2A,
and operator/worker robustness. These names organize the work; passing tests or
local Docker evidence do not establish an independent-host/laptop-off run.

Human-issued bounded authority is consumed by agents, not impersonated by them.
Grants must remain bounded by scope, expiry, revocation, budget, destination,
and delegation fences. Real reasoning and real independent A2A peers are distinct
from deterministic coordinator/provider-disabled coverage. No mock evidence is
to be presented as live integration or scientific validity. No secret values or
private reasoning are recorded here.

The shared task intent (task `tessera-remote-agent-platform`, revision 1) defines
the acceptance checks: `authority`, `notebook`, `remote-runtime`, `full-mcp`,
`a2a`, `robustness`, `integrated-journey`, and `journal-install`. This initial
entry records no pass for any of them.

## Lane plan and ownership

The coordinating parent has launched the following disjoint work lanes and
ownership areas. Interfaces should converge on typed
tenant-scoped contracts, durable IDs/revisions, explicit authority checks,
correlated event/provenance records, and truthful status/unknown outcomes.

| Lane | Intended boundary / interface | Acceptance focus |
|---|---|---|
| Authority/schema/store/routes | Existing schema/store/models/API/experiments/execution/external-agents/`research.py`/pyproject plus new `agent_authority` | Human-issued bounded grants and agent-scoped typed project/study/experiment proposals, observations, review requests and notebook/export routes; transactional tenant/idempotency/budget/revision/expiry/revocation enforcement; negative tests; no authority widening |
| Provenance module | `src/tessera_control/agent_provenance.py`, `public_capture*`, `agent_provenance_models.py`, notebook projection, runtime shared client/outbox | Schema-52 installed receipt remains historical. Schema-53/54 source adds public-output registration/capture/A2A joins and typed metadata, but V2 runtime emission and independent generated reasoning remain unverified |
| Supervised reasoning runtime | `src/tessera_agent/remote_reasoning_runtime.py`, `remote_reasoning_store.py`, `src/tessera_mcp/guard_phase_evidence.py`, observer adapter, `docs/research-consumer.md` | V6 adds explicit opt-in evidence-backed admission v2 while strict v1 remains default. Python verifier/factory and isolated native terminal have local interop receipts (detailed below); upstream resource fixtures are synthetic. Historical 4,268 gate and later exact-wheel receipts do not cover v6 source. | No genuine enrolled principal/owner, trusted operator/current-financial resolver, private guard/quota adapters, durable native grant/budget claim, installed bridge or real model session/admission. Current source full gate/package acceptance remains pending. |
| Governed MCP | `src/tessera_mcp/server.py`, `mcp_workflow.py`, `docs/mcp-full-workflow.md`, `scripts/check_mcp_control.py`, `scripts/check_mcp_full_workflow.py`, `scripts/check_mcp_workflow_local.py` | Installed schema-52 normal journey remains historical; separate exact-wheel local protocol run passed MCP stdio catalog/scoped operations and reconnect/replay against the RC wheel using synthetic fixtures. Full research harness discovery/experiment/review is not established |
| A2A | `src/tessera_control/a2a_peer.py`, `a2a_store.py`, `docs/a2a-peer.md`, `tests/test_a2a_peer.py`, `tests/test_a2a_store.py`, `dev/check_a2a_fixture.py`, `dev/check_a2a_peers.py` | A2A 1.0.0 / SDK 1.2.1; installed peers replayed and each drained four causal metadata records, joined to notebook; reference-only, no reasoning |
| Worker + desktop robustness | Withdrawn-context discovery; owner-fenced namespace Pod pagination; atomic connection-bound desktop artifact save | Local targeted acceptance reported passed for all three; no live cluster/native picker proof |
| Platform journey harness | `dev/remote_agent_platform_live.py`, `tests/test_remote_agent_platform_live.py`, `docs/remote-agent-platform-journey.md` | Fresh schema-52 16-stage run/eight ACKs remain historical. The v4 workspace blocker was resolved; isolated model-hook executor, SDK channel and Tessera observer/factory join now have bounded local receipts. Exact-wheel local MCP/A2A compatibility passed, but uses synthetic output/identity and does not generate reasoning |
| Governance/operator docs | `DESIGN.md`, `README.md`, `CONTRIBUTING.md`, `docs/remote-agent-platform-install.md`, backlog and this journal | Contract and ordinary install path; explicitly label backend/runtime/peer gaps and require post-convergence command/version reconciliation |

The authority interface is frozen in `docs/agent-authority-interface.md`; this
is an interface decision, not implementation or test evidence. Provenance
interface decisions are recorded in `docs/agent-provenance-interface.md`.
The runtime module boundary is now published in
`docs/remote-reasoning-runtime.md`; however, no secure agent credential, dedicated
approved included-OAuth research profile, or named independent host was handed
off. Local Docker integration is authorized but must wait until concurrent
writers quiesce; no deployment or Docker action is claimed here.

MCP uses the already-pinned SDK 1.26.0, so this lane has no dependency change
planned. MCP analysis found the backend does not yet expose the agent-scoped
project/study/experiment proposals, observations, review requests, and
notebook/export surface needed for the full workflow. The authority backend lane
now owns those typed routes and existing `research.py` integration; this is a
reported gap and assignment, not a completed route or a reason to proxy a human
token or impersonate a human decision. Provenance integration remains pending;
the interface hooks are specified in `docs/agent-provenance-interface.md`.

Integration must keep state/event/idempotency writes atomic, tenant checks at
data access, and execution authority at the supervisor boundary. The repository
design describes required behavior, not proof it is implemented. Missing
implementation is a work item, not a permanent prohibition.

## Milestones and validation ledger

### 2026-10-04 — Initial journal and scope reconciliation

- Read shared intent revision 1 and its eight check IDs; no check evidence was
  supplied in that intent beyond the acceptance descriptions.
- Read `AGENTS.md`, the `tessera-change` workflow, applicable CONTRIBUTING.md
  verification gates, `DESIGN.md` scope/status, and the opening/current-evidence
  sections of `IMPLEMENTATION_TASKS.md`.
- Existing implementation backlog states its laptop-independent objective is
  open. Its dated Docker Desktop Kubernetes receipts describe provider-disabled
  local-cluster runs with quarantined artifacts and pending human review; they
  do not satisfy independent-host, laptop-off, provider-enabled reasoning, or
  scientific-validity acceptance. Those are prior-recorded evidence, not checks
  rerun by this journal author.
- The current working tree contains extensive pre-existing modifications and
  untracked implementation/docs/tests. Preserve them; do not treat their
  presence as verification or alter them in this journal-only assignment.
- Launched lane ownership is now recorded above and in the additive backlog
  crosswalk below. Authority and provenance interface documents exist; the
  authority interface is reported frozen. Implementation/tests remain pending.
- Initial journal-only validation: `git diff --check --
  docs/remote-agent-implementation-journal.md` passed. No runtime, full test,
  MCP, A2A, live-reasoning, or Docker checks were run by this journal lane.
- Backlog crosswalk added at the end of `IMPLEMENTATION_TASKS.md`; bounded reads
  confirmed it follows the existing walkthroughs and leaves prior sections and
  historical task checkboxes in place. `git diff --check --
  IMPLEMENTATION_TASKS.md docs/remote-agent-implementation-journal.md` passed.
  The backlog file was already modified in the pre-existing working tree; its
  full pre-existing diff was not treated as authored or validated by this lane.
- No runtime, full test, MCP, A2A, live-reasoning, or Docker checks were run by
  this journal lane; local integration remains deferred until concurrent writers
  quiesce.
- Blockers: no named independent off-host target or fresh provider credential /
  spend scope is in the shared intent. This blocks those live runs only, not
  implementation or authorized local checks. Unnamed writer activity also means
  local Docker must wait for a quiescent tree. Next action: parent/coordinator
  continues lane implementation, then records exact prerequisite/runtime
  reference needs without exposing credential material.

### 2026-10-04 — MCP backend seam and integrated journey harness assigned

- MCP reports the installed extra already pins SDK 1.26.0; no dependency change
  is needed. The backend currently lacks the agent-scoped project/study/experiment
  proposals, observations, review requests, and notebook/export routes for the
  requested full workflow. The authority backend lane now owns those typed routes
  and `research.py` integration. This is a reported gap/assignment, not a claim
  that the routes exist, and does not authorize a human-token proxy or agent
  impersonation of human approval.
- The provenance lane has published exact proposed hooks in
  `docs/agent-provenance-interface.md`; integration remains pending.
- A journey-harness lane owns `dev/remote_agent_platform_live.py`,
  `tests/test_remote_agent_platform_harness.py`, and
  `docs/remote-agent-platform-journey.md`. It is building an orchestrator for
  installed components; no run has occurred and harness code is not live proof.
- The initial journal/backlog scoped diff check passed. This is documentation
  hygiene only, not completion of `journal-install` or integrated acceptance.
  Planned installed-workflow and design/docs reconciliation remains after lane
  implementations and writer quiescence. No Docker deployment/run is claimed.

### 2026-10-04 — Governed-agent design and install documentation

- Updated `DESIGN.md`, `README.md`, and `CONTRIBUTING.md`; added
  `docs/remote-agent-platform-install.md`; linked these boundaries from the
  additive `IMPLEMENTATION_TASKS.md` crosswalk. The design now specifies distinct
  agent identity, human-issued bounded grants, subset/atomic delegation,
  correlation and completeness semantics, supervised real reasoning, supported
  stdio MCP, and independent-peer A2A. It expressly separates contract from
  verified implementation.
- The runbook uses actual current package metadata (`tessera-mcp`, optional MCP
  SDK 1.26.0, session stdio protocol 2025-11-25), and existing installation guide
  commands. It does not invent a credential-enrollment command, A2A peer
  entrypoint, reasoning-runtime service command, schema number, or private
  receipt/header. The MCP backend-route gap and the runtime/A2A/install gates are
  marked for reconciliation after implementation convergence.
- Scoped validation passed: `git diff --check -- DESIGN.md README.md
  CONTRIBUTING.md IMPLEMENTATION_TASKS.md`; a local Markdown check confirmed
  relative link targets and trailing-whitespace-free content across those four
  files plus this journal and the new install guide. This validates doc hygiene,
  not a fresh wheel install, live MCP, real reasoning, A2A compatibility, or full
  `journal-install` acceptance.

### 2026-10-04 — Provenance module and scoped robustness leaf evidence

- Provenance module owner reports implementation in
  `src/tessera_control/agent_provenance.py`,
  `src/tessera_control/agent_provenance_models.py`, and
  `tests/test_agent_provenance.py`, with interface details in
  `docs/agent-provenance-interface.md`. Their exact focused result was
  `pytest -q tests/test_agent_provenance.py`: **15 passed** using local SQLite
  and synthetic records. Scoped Ruff lint/format and diff checks also passed.
  These results cover the module, not its integration. Schema/bootstrap hooks,
  transactional denial audit, authorized notebook/export joins and byte
  accounting remain pending. The same lane is implementing the separate
  scoped `notebook:read` agent projection; no result for that work is recorded.
- Worker/desktop robustness owner reports changes in
  `src/tessera_worker/kubernetes_pod_listing.py`,
  `src/tessera_worker/kubernetes_remote_service.py`,
  `src/tessera_worker/kubernetes_output_collector.py`, their
  `tests/test_kubernetes_pod_listing.py` and
  `tests/test_kubernetes_remote_service.py` coverage, and
  `desktop/artifact-save.mjs`, `desktop/connection.mjs`, `desktop/main.mjs`,
  `desktop/test/artifact-save.test.mjs`. Reported focused coverage: **59 Python
  tests plus 3 consumer regressions**, then **49 desktop boundary tests plus 2
  new negative tests**. Scoped Ruff, npm run check, JavaScript syntax and diff
  checks passed. Python Kubernetes/dialog behavior was synthetic; desktop save
  exercised the actual local filesystem. This is self-reported leaf evidence,
  not independent verification or live cluster/UI evidence.
- This does not complete aggregate `robustness`: the sibling withdrawn-context
  list-poisoning repair remains pending. A real namespace, Kubernetes API and
  native picker were not exercised. The native UI sequence remains unverified:
  agent credential → Open picker → released artifact preview → Save picker →
  verify saved bytes/hash → reconnect. Record each actual device/operator step
  only after it is run.
- The earlier docs-only scoped diff/link/whitespace checks passed. They are
  separate from runtime leaf results and do not complete `journal-install`;
  fresh installed-agent workflow, final cross-doc reconciliation and broader
  applicable gates remain open.

### 2026-10-04 — MCP, reasoning runtime, notebook projection, and journey-harness updates

- MCP owner reports 15 typed tools in `src/tessera_mcp/server.py` and
  `src/tessera_mcp/mcp_workflow.py`, 38 focused synthetic tests plus one schema
  test, scoped Ruff checks, and an initial real SDK/control proposal/context/
  reconnect/denial run. That initial pass is bounded proposal coverage, not full
  MCP acceptance. The latest standalone rerun stopped before protocol calls when
  fresh control `/health/ready` returned **503 `schema_mismatch`** during schema
  integration. The MCP workflow is documented in `docs/mcp-full-workflow.md`;
  `scripts/check_mcp_control.py` and `scripts/check_mcp_full_workflow.py` exist.
  Backend routes and the complete grant/research/notebook/export journey remain
  pending; preserve the readiness failure and resume the same live-check lane.
- Reasoning owner reports `src/tessera_agent/remote_reasoning_runtime.py`,
  `src/tessera_agent/remote_reasoning_store.py`,
  `tests/test_remote_reasoning_runtime.py`,
  `scripts/check_remote_reasoning_live.py`, and
  `docs/remote-reasoning-runtime.md`, with **25 synthetic tests** and scoped
  checks/help passing. During this documentation pass, `pyproject.toml` was
  inspected and found to register the `tessera-reasoning` console entrypoint
  alongside the documented module invocation; a fresh wheel
  build/install and invocation of the installed entrypoint remain unverified.
  No secure enrolled-agent token reference or
  dedicated approved included-ChatGPT-OAuth research harness profile was handed
  off; no named independent host exists. The existing coding-agent service was
  not reused and no inference was called. Its token cap is observed-usage, not a
  hard pre-token ceiling; interrupt acknowledgement is not provider termination.
- Provenance owner reports **27 local SQLite tests**, including 12 additional
  `notebook:read` projection/authorization cases, and scoped Ruff/diff checks.
  Agent projection implementation is present, but backend action `Literal`,
  enrollment, routes, schema/bootstrap, denial-audit and authorized export/byte
  cap integration remain pending. This supersedes the earlier “module tests 15”
  snapshot without deleting that historical milestone.
- Journey harness now owns `dev/remote_agent_platform_live.py`,
  `tests/test_remote_agent_platform_live.py`, and
  `docs/remote-agent-platform-journey.md`. Its owner reports **19** focused
  safety tests, but no services were started and no live journey ran. Replaying
  with a fresh MCP subprocess/session discards client state; it is not a network
  fault or remote service restart proof. Same lane is inspecting/adapting to
  current local interfaces; no replacement harness is needed. Real restart
  generation, retained-output bytes, independent A2A peer and joined complete
  export remain prerequisites.
- All implementation/test counts and focused command outcomes above are
  self-reported leaf evidence, not independent verification. Do not promote them
  to full local integration, A2A interoperability, live reasoning, or
  laptop-off acceptance. Missing secure references/host and the 503 readiness
  failure have concrete next actions recorded above; implementation remains
  authorized and not prohibited.

### 2026-10-04 — A2A peer completion and latest operator preflight reconciliation

- A2A peer owner reports the official **A2A 1.0.0** JSON-RPC subset using
  `a2a-sdk[http-server]==1.2.1`, with implementation in
  `src/tessera_control/a2a_peer.py` / `a2a_store.py`, contract in
  `docs/a2a-peer.md`, tests `tests/test_a2a_peer.py` and
  `tests/test_a2a_store.py`, and opt-in runners `dev/check_a2a_fixture.py` and
  `dev/check_a2a_peers.py`. The reported local live test used two independent
  real peer processes, two SDK clients and actual control HTTP. Discovery,
  send/get/result, restart/replay, cancellation/late-result, authentication,
  cross-peer/stale-epoch denial, ancestor revocation and real-time expiry replay
  denial passed. Test report: 10 SDK/store tests plus the body-limit test; default
  tests 4 passed/1 optional SDK skip; scoped Ruff passed. Provisioning/records
  were synthetic. This is bounded local protocol interoperability, not autonomous
  critique, live reasoning, off-host operation or joined notebook provenance.
- Current `pyproject.toml` and `uv.lock` now include optional `a2a` SDK pin and
  `tessera-a2a-peer`; no fresh wheel/installed command check was made. The older
  packaging-pending paragraph in `docs/a2a-peer.md` is stale and needs
  reconciliation. A2A bridge implementation is underway in
  `src/tessera_agent/a2a_reasoning_bridge.py`, with approved task-profile/digest
  mapping and redacted outbox (25 focused tests reported); the peer itself does
  not supply that reasoning. Runtime owner is adding
  `src/tessera_agent/provenance_client.py` plus durable outbox delivery, and the
  bridge owner is implementing collector delivery through it. Actual collector
  delivery and central notebook integration remain pending.
- Reasoning owner reports **26** synthetic tests (updating the earlier 25-test
  milestone) and scoped checks/help; authentication to OpenCode is HTTP Basic
  while Tessera control remains scoped bearer. Guarded OpenCode V2
  `2.0.18-subscription.20260929` was inspected, not used for inference. Separate
  process/workspace is possible, but the inspected service shares the owner's
  account database and lacks a dedicated research profile. Do not use the coding
  service. An isolated local agent credential can be minted by separately
  authorized human setup; the missing live prerequisite is the dedicated,
  approved included-OAuth research profile/private references and a named
  authorized host. No credential values or auth stores were inspected.
  Usage caps remain observational, not hard pre-token billing caps; interrupt
  acknowledgements do not prove provider termination.
- Journey owner reports **31** schema/safety tests and a read-only UID-fenced
  generation-baseline probe passed; no service restart was performed. Latest
  read-only preflight found the TLS route expired/no listener and ordinary worker
  unready with **36 restarts**. The selected installed wheel does not contain the
  new modules; planner credential references are placeholders; dedicated
  reasoning references are absent; the historical grant is already reconciled
  and is not fresh authority. Same owner is diagnosing read-only and adding setup
  orchestration. No service restart, fresh grant mutation, job, inference or
  replacement harness was run by this journal lane.
- The old `docs/a2a-peer.md` pending-packaging statement and current source
  metadata/lock now disagree; reconcile the doc and build/install a fresh
  artifact before claiming ordinary installed-peer availability. Preserve the
  earlier MCP 503 readiness event separately. Full MCP, A2A-to-reasoning, scoped
  provenance append/export, real runtime profile, service restart and integrated
  user journey remain open.

### 2026-10-04 — Schema-51 backend and installed-service profile evidence

- Authority/backend owner reports **63 targeted tests passed in 103.07s**,
  covering schema-51 fresh state and explicit v50→v51 migration/readiness 200,
  human-issued bounded authority, transactional consume/delegation, typed MCP
  research workflows, provenance including rollback-surviving denials, human
  same-snapshot exports, and withdrawn-context discovery repair. This is focused
  temporary SQLite/in-process HTTP coverage. Combined broader research/experiment/
  Kubernetes runs timed out at 120s and 300s without a reported test failure;
  they are incomplete, not passing evidence.
- Backend registered `agent_provenance_api.router` and
  `app.state.agent_provenance = AgentProvenanceStore(state_dir)` against schema
  51. Explicit `provenance:append` scope (maximum nine actions) is required;
  existing enrollments gain no inferred capability. Four focused application
  HTTP tests passed in **8.80s** for grant-bound append/replay/conflict,
  notebook/export and scope/destination/project/subject/revocation denial. This
  did not bump schema again. Runtime/A2A outbox delivery remains pending.
- Provenance module owner reports **44 module tests**. The scoped
  `notebook:read` projection and HTTP collection integration now exist; complete
  runtime/A2A delivery into the collector and full authorized notebook/export
  acceptance remain open.
- Ordinary Linux service profile is added under `deploy/remote-agent-platform/`,
  with `src/tessera_agent/service_startup.py` and
  `tests/test_remote_agent_services.py`. The owner reports **57 passing tests**
  (31 service-profile and 26 runtime), including hardened systemd
  `LoadCredential` mapping. No Linux/systemd host execution or fresh wheel install
  was performed, so service lifecycle and installed entrypoints are not live
  acceptance.
- A2A reasoning bridge `src/tessera_agent/a2a_reasoning_bridge.py` has 25
  focused tests reported for exact approved task-profile/digest/context fences,
  durable runtime-task linkage and redacted outbox. Outbox delivery is not yet
  observed. Runtime owner is adding a shared provenance client/outbox; bridge
  owner is integrating it. Preserve delivery and authorized notebook projection
  as pending.
- MCP owner resumed the full SDK/control scenario using isolated human API
  enrollment/grant setup; no user owner-token handoff is required. No final
  complete-scenario result is recorded. The earlier 503 `schema_mismatch` remains
  a historical blocked attempt; backend schema-51 readiness 200 is separate and
  is not proof that the full MCP journey passed.
- The new install bundle and this journal make schema 51/migration 51 explicit.
  Continue to distinguish component test results from timed-out combined
  regressions, Linux service execution, fresh-wheel install, runtime/A2A delivery,
  autonomous critic behavior, and independent-host acceptance.

### 2026-10-04 — Desktop gate and expired ordinary-worker credential follow-up

- The desktop leaf reports a quiescent integration gate:
  `npm test --prefix desktop` = **205 passed**, 0 failed/skipped/cancelled in
  **3.618s**; `npm run check` and scoped diff checks passed. Its packaged Electron
  hidden-window smoke reported sandbox/context isolation enabled, Node integration
  disabled, zero renderer errors, null status and exit 0. The stale assertion in
  `desktop/test/journey-bridge.test.mjs` was corrected for the staged Save helper.
  The 41 desktop root/test-file tree had SHA-256
  `828be9665891ccad6bc6717f4967347d78377fcf44419d76b3e8cf4460ce2a33` and was
  unchanged during the final gate. No separate desktop build script exists.
- That test host used Node **22.2.0**, below the documented **22.12.0** minimum;
  tests and the hidden-window packaging smoke are observed, but a supported Node
  installation is not established. Native OS picker/full connected UI remains
  unverified: credential → Open picker → released artifact preview → Save picker
  → verify bytes/hash → reconnect. Fallback file selection is not picker proof.
- The aggregate local `robustness` checkpoint now reports passed for the three
  reviewed repairs: withdrawn-context discovery (included in backend's 63-test
  gate), owner-fenced pagination (59 targeted Python tests plus 3 consumer
  regressions), and connection-bound atomic artifact save (desktop gate above).
  This does not establish a live Kubernetes API, physical/native picker journey,
  or full `journal-install` acceptance. Earlier milestone notes that the
  withdrawn-context fix was pending remain as historical state at their time;
  this later evidence supersedes them.
- Journey harness now reports **43 focused tests** (**5.55s**) covering schema
  adaptation and automated isolated human-API planner/critic enrollment,
  optional fresh-target worker registration, status-only child delegation, peer
  configuration and expected agent self-enrollment 403. No services or Jobs
  were mutated. A read-only human API/status check confirms the existing worker
  credential expired at **11:54:43Z**; subsequent GET/status returned 401,
  `ControlChannelError` was logged and the worker showed **39 restarts**. An
  expired worker credential cannot be renewed. This is a newer observation than
  the prior preflight's 36 restarts; both remain in history, not conflated.
- Planned repair is a fresh worker identity/journal path that preserves the old
  expired identity and uncertain operation history. No old token renewal, service
  restart, Job dispatch or cleanup mutation occurred. The earlier TLS route
  expired/no-listener observation remains separate. Fresh-worker setup is
  implementation work, not a permanent prohibition or a reason to erase history.

### 2026-10-04 — Bounded real MCP SDK/control workflow passed

- MCP owner reports **29 real SDK/stdio/control calls passed** through proposals,
  separately human-issued authority request/grant, admit/dispatch/status/cancel,
  observation/review, dual-cursor notebook/export, reconnect replay, denial,
  revocation and expiry. The separately authenticated human setup used HTTP/CLI;
  no owner token was proxied to the agent. The exercise used real MCP SDK child
  processes and control HTTP, not in-process mock transport.
- The pass used synthetic package/dispatch-queue resources only. No executor,
  worker, Kubernetes cluster, inference, paid provider call, or real experiment
  was run; fixture processes were cleaned up. This is a passed local
  protocol/control-surface boundary, not ordinary installed-wheel, external
  execution, scientific, A2A-critic or integrated-researcher acceptance.
- `scripts/check_mcp_workflow_local.py` now holds the local scenario; the MCP
  owner reports 46 targeted tests plus scoped checks passing and the final
  standalone control harness passing. The dual-cursor notebook pagination defect
  was fixed. Preserve the earlier 503 `schema_mismatch` run as a failed historical
  attempt; the later schema-51 backend readiness and this protocol run are newer
  and distinct evidence.
- Remaining MCP boundary: exercise a fresh installed wheel and ordinary service,
  then complete worker/execution convergence. Runtime/A2A local outbox delivery
  implementation is reported in the next milestone; ordinary TCP service
  acceptance, fresh worker identity after expiry, full Python gates, A2A critic
  inference, actual reasoning and final integrated journey remain open.

### 2026-10-04 — Shared provenance outboxes and final-gate repair sequence

- Runtime owner reports **58 targeted tests** for the shared
  `src/tessera_agent/provenance_client.py` and durable runtime outbox; scoped
  checks, help and diff checks passed. The outbox commits stable, content-free
  provenance atomically with runtime transitions and clears only on an exact
  collector acknowledgement. `httpx` is now an ordinary declared/locked runtime
  dependency. Collector routes and SQLite were exercised through an in-process
  synthetic transport, not TCP to an ordinary service; no real reasoning was run.
- A2A bridge owner reports `--deliver-provenance` terminal drain support and
  **34 focused tests**. Local HTTP/SQLite checks passed joined notebook
  append/replay, matching receipts and atomic acknowledgement; denied or unknown
  outcomes remain frozen/pending. The client strips private links before append.
  This is local collector/bridge evidence, not autonomous critic inference.
- First broader source-gate attempt used revision digest
  `32c4...` (recorded by the owner): lint/format reported **562 passed**; shard 0
  reported **677 passed, 1 failed, 1 skipped** in 679.7s; shard 2 reported **587
  passed** in 797.2s; shards 1 and 3 timed out at 900s. The failed tests exposed
  agent status routes unconditionally using the human-only projection and a
  quota fixture expecting 3 rather than 2. Legacy agent resource/status exports
  were repaired to disclose an explicit notebook-unavailable/withheld marker
  without widening scope; real human project exports remain intact. Test setup
  also preserves the intended three Jobs/four Pods instead of cleaning those
  resources. The focused privacy/preflight regression set then reported **16
  passed in 27.75s** at source digest
  `4e0319220ac216404ea0d972debb7e7c4c246ad2cb8251e900fef4491f4cedfa`.
- Next broader attempt: lint/format **563 passed**; shard 0 **678 passed, 1
  skipped** in 463.32s; shard 1 **806 passed, 1 failed** in 847.72s, with 249
  tests unrun in that shard; shards 2 and 3 were not started. The remaining
  regression exposed migration clearing `held_restore` forensic backup metadata.
  Repair now invalidates backup freshness only when it belongs to the current
  active recovery state; held metadata/history and mutation denial remain intact.
  The focused migration/recovery suite reports **9 passed in 43.35s** at source
  revision `8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9`
  (404 files, 5,794,639 bytes). The wider frozen gate subsequently completed on
  this exact unchanged revision; that baseline is recorded in the next milestone.
- At that checkpoint, source writers reported quiescent after those repairs and
  no remaining gap in their then-assigned changes. The subsequent installed
  journey has exposed a separate known queued-dispatch recovery gap; see the
  next handoff milestone. This is not platform source completion.

### 2026-10-04 — Frozen source baseline and installed recovery handoff

- The frozen source gate has now completed sequentially on unchanged revision
  `8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9` (404
  files, 5,794,639 bytes; before/after digests equal; no source edits during
  gate). Ruff check passed and Ruff format covered **564 files**. Shards 1/2/3/0
  reported respectively **1056/591/1129/678 passed**, totaling **3454 passed,
  0 failed, 1 skipped**; pytest times were **1169.32/556.51/979.35/502.73s**.
  The single skip was `tests/test_a2a_peer.py` because the optional pinned A2A SDK
  was absent from this gate environment. Frozen receipt:
  Local evidence record ID `SOURCE-GATE-RECEIPT-INTENT8`.
  This is a valid local executable baseline for that digest, not proof of the
  later execution-recovery edits or live journey.
- A subsequent clean installed platform artifact used wheel SHA256
  `1c74141cab11fc468800aee21467f5c5ef8c7c30086555ba5c6aa10c13ec5686` and image
  `sha256:d318a56424abe080967b3efb0e75ffa2237bb25d3e0e492b89d74b2a4be18bf2`;
  it preserved a schema-50 backup and migrated the installed control service to
  schema 51. Its partial journey passed proposal/critic setup, authority request,
  separate human issue, admission and control restart/revocation observations,
  but package returned 409 after its short lease expired; dispatch was unknown
  and not retried. No Job was submitted. Notebook traversed only a partial page
  (sequence 8/event cursor 9190, `has_more` true). Receipt:
  Local record ID `REMOTE-PLATFORM-PARTIAL-2026-10-04`.
- A distinct authorized two-hour installed run used fresh, separately enrolled
  identities while preserving prior journals. Its standalone checks included two
  installed A2A 1.0 JSON-RPC peers (SDK 1.2.1), each passing delegated
  submit/get/result, replay, changed-key/stale-epoch denial, cancel and
  cross-ledger denial; results were reference-only, with no reasoning. MCP review
  succeeded, but an observation was denied with no authority effect. Six notebook
  pages contained stream metadata only and exhausted the cursor (sequence 8/event
  cursor 9275) while completeness remained `partial_page`, with historical/off-platform
  work unknown. One expired
  lease/admission and zero operations/dispatches still held the concurrency
  reservation; ordinary human reconcile and new grant returned 409. The two-hour
  run credentials expired at 21:14 UTC. The **45-test** setup suite passed in
  15.56s; scoped Ruff/format/diff checks passed. Receipt:
  Local record ID `REMOTE-PLATFORM-RUN2-BLOCKED-2026-10-04`.
- The no-package recovery gap was then implemented in `execution.py` and installed
  in a new wheel SHA256 `116006bf571e78122b96474c379ec2799d44d837abefd6d5117ea5bf385c506a`
  / image `sha256:42739147450ce86901a76ea872594d78edf5b2de1cf79b623c4c54e033155e3a`.
  Its source revision is `2d93d6ecc1fe8134d4aa4cf82e85ab8bbf79dc7f4e180292a5e1a1c95c1d879c`;
  the owner reports 13 new recovery cases plus 14 existing lifecycle cases
  passed (13 in 53.42s; 14 in 24.89s), and 27 targeted installed recovery/lifecycle
  checks. Scoped Ruff/format/diff checks passed. Human recovery
  returned 200 and same-key replay was identical; installed history retained one
  lease/admission and recorded `not_executed` with zero operations, no package or
  outbox, and **no provider-termination inference**. This clears a reservation
  only with the explicit proof/guards; it is not queued-dispatch recovery. The
  earlier frozen 3454-test gate predates this source revision; a new full gate
  has not run. Sanitized receipt:
  Local record ID `REMOTE-PLATFORM-RECOVERY-EXECUTION-2026-10-04`.
- The subsequent installed journey attempt reached human issue, admission,
  package 201 and dispatch, but retained-output retrieval failed. Preparation had
  omitted `network_none_evidence` from the worker identity digest: actual
  `205911d6dc13f519e4b946cf92297978b574699f9c50039ceb9f66548a717339` differed
  from dispatched `65dc37b46a1193858ea873a203b8e91c278b8a5474ce1bcd7ae2db365f45d2dc`.
  The prep path was corrected to include the witness and to admit before package;
  already-issued immutable binding was not rewritten. A human-fenced cancel is
  `cancel_requested`, not reconciled. The exact expected Job lookup found zero
  matching Jobs (five historical project Jobs); Job UID/output digest are null.
  MCP review still passed; observation remained blocked with no authority effect;
  six-page notebook stream contained metadata only, is exhausted but `partial_page`, not complete
  experiment evidence. The handoff explicitly requires queued-dispatch recovery
  through the original worker/control with provider-absence proof; do not use SQL,
  fake termination, or infer release from absence alone. Receipt includes the
  failed journey and recovery handoff. No real reasoning or external-host proof.
- The journal/dashboard last showed quota telemetry timing out; the root later
  refreshed telemetry as healthy with roughly **35%** remaining at 19:06 UTC and
  resumed the same child with preserved resources. This is not quota exhaustion;
  no paid inference fallback was used. New edits after the frozen baseline must
  not be represented as covered by that baseline.

### 2026-10-04 — Expired-worker stop-only human maintenance recovery

- The original-worker never-claimed path passed 28 isolated cases and was installed.
  The subsequent actual operation Job-list GET returned `403`; the original bearer
  then returned `401` after its 21:14 UTC expiry. The original worker is stopped,
  workspace custody/journals retained, grant still cancel-requested; no complete
  proof, attestation, release, Job/output or termination success was observed. See
  Local record ID `REMOTE-PLATFORM-QUEUED-EXPIRY-BLOCKED-2026-10-04`.
- Added typed, event-backed five-minute maintenance issuance and human proof
  consumption for the separately authenticated **original human grant issuer**.
  Exact original expired principal/credential, grant/operation/dispatch/package,
  revision/epochs, namespace/actual target, custody digest, provider-read reference
  and actual human session are bound. No worker credential, execution, renew,
  dispatch, spending or Kubernetes RBAC authority is created. Schema v51 is unchanged.
- The collector's `--maintenance-file` mode has no worker bearer. It checks the
  actual target before provider reads, retained scanner/effect history and durable
  local insert fences, then requires fresh complete actual provider absence. A
  `403`, incomplete read, claim/intent/helper/observation/receipt/provider identity
  remains held. Human consumption atomically appends distinct human-attributed
  `not_executed` events and releases zero settlement once, never termination.
- New isolated maintenance coverage: **30 distinct cases passed across scoped
  runs**, including foreign actors/credentials/tenants, expiry/scope/digest/custody
  denial, actual provider-403 propagation, no bearer/authority widening, concurrent
  authorization issuance and release, accounting rollback, and redacted human
  notebook export. Provider callbacks are synthetic. Two affected original-worker
  compatibility cases passed (11.50s); scoped Ruff, formatting, diff and CLI-help
  checks passed. No live effect or new full gate was run in this source repair.
- Files: `queued_maintenance.py`, `queued_recovery.py`, execution models/API/store
  methods; `kubernetes_maintenance_recovery.py`, queued collector CLI;
  `test_kubernetes_queued_maintenance.py`, DESIGN and the
  [exact maintenance operator runbook](queued-dispatch-maintenance.md).
- At the time of this source-only checkpoint, installation/read authorization and
  live proof were still pending. The following installed-maintenance/run-5 entry
  supersedes that next-step status while preserving the original blocked receipt.

### 2026-10-04 — Installed human maintenance and distinct run 5

- Preserve the earlier blocked receipt: the original worker first authenticated
  at 21:08:40Z; its credential expired at 21:14:03Z. Operation-scoped Job LIST
  returned `403`, then the original bearer returned `401` at 21:14:21Z. No
  complete absence proof, attestation, human release or termination conclusion
  was then recorded; the original worker was stopped and its workspace/journals
  retained. Receipt:
  Local record ID `REMOTE-PLATFORM-QUEUED-EXPIRY-BLOCKED-2026-10-04`.
- A separately authorized human operator then provisioned the narrowly scoped
  namespace-only Job/Pod `get,list` role/read reference and ran event-backed,
  five-minute maintenance as the original human grant issuer. Installed maintenance
  issuance returned **201**; actual proof reconciliation returned **200** with
  identical same-key replay and `reconciled`. The expired worker credential was
  not revived. The maintenance authorization itself created no bearer, worker,
  execution, renewal, dispatch or provider permission; a separate temporary
  service-account token was used only with the separately authorized, namespace-
  UID-scoped Job/Pod read Role. No termination was inferred. The stopped original
  workspace and histories remain preserved. Receipt:
  Local record ID `REMOTE-PLATFORM-MAINTENANCE-RUN5-2026-10-04`
  (SHA256 `850a9a03728b0c72f812d68ca914420eace25a0fccd0a28ecae56b7783d96bbd`).
- Distinct installed run 5 used fresh worker/planner/critic credentials and new
  workspace. **All 16 configured journey stages** passed as observed protocol and
  control assertions, including real SDK dispatch replay, restart generation,
  expansion `403`, expiry `403`, revoke-epoch `409`, and output collection. A real
  Job produced a **36-byte** artifact with SHA256
  `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`; authorized
  readback matched expected public CPU value **385**. The Job completed after its
  control lease had been cancelled, so the output was late/quarantined; human
  disposition retained quarantine, and the supported resolution is `cancelled`,
  not successful execution or scientific validation. Do not count the configured
  stage pass as operational success.
- Installed MCP observation/review passed. A separate MCP pre-submission
  admit/status/cancel/reconnect path passed; it involved no lease/package/Job and
  is not a live-running-Job termination claim. Final eight-page export exhausted
  at sequence **50** / event cursor **10877**, but remains `partial_page` and
  redacted metadata-only. The output manifest ID is absent from that notebook
  export; correlated critic outbox delivery is unverified. Critic results remain
  reference-only. Export and job receipts are in `installed-maintenance-run5.json`.
- **Correction to the previous remaining-items hypothesis:** the ordinary
  installed worker already renews leases; missing renewal is not an implementation
  gap. `KubernetesDispatchWorkerService` renews every 10s with stable journaled
  actions through `POST /worker/leases/renew`, enforcing the grant epoch and
  immutable hard expiry. See
  [`ordinary-worker-renewal-diagnosis.md`](ordinary-worker-renewal-diagnosis.md).
  No worker/backend/runtime code was changed for this diagnosis.
- Selected worker-lifecycle tests report **24 passed, 38 deselected**, two
  dependency-deprecation warnings, **115.45s** pytest time. The outer shell reached
  its 120s timeout after the completed pytest summary, so no shell exit code was
  observed; this is a completed selected pytest summary, not a full-suite pass or
  a false incomplete-test count. Scoped Ruff lint/format and new-document whitespace
  checks passed. Tests used temporary SQLite, in-process API handlers, fake
  Kubernetes effects and controlled clocks; no installed/live Job was rerun.
- At this checkpoint the run-5 receipt lacked a renewal/cancellation timeline. The
  later schema-52 read-only snapshot provides zero renewal actions, terminal worker
  exit at 21:53:20.499345Z, lease expiry at 21:53:32.979713Z, manifest recording at
  21:53:34.220823Z and later human cancel at 21:55:11.242538Z. Manifest recording
  was after expiry; the later cancel was not the cause of initial lateness. Exact
  cancellation reason/actor and why no renewal action was recorded remain unknown.
  Do not claim renewal is missing or infer closed reason. Earlier timeline collection
  was blocked by broad deployment ownership, **not authorization denial**.
- Normal acceptance sequence: target/network/input preparation; acquire the short
  lease; freeze its epoch with final pins; admit; issue the immutable package and
  dispatch before the initial lease expiry. Keep grant/task/credential active while
  the ordinary worker runs, renews, reports terminal evidence and collects/uploads
  output. Do not run expiry/revocation/cancel stages on this happy-path attempt;
  first require supported success, `late=false`, retained manifest/digest and
  committed resolution. Exercise expiry/revocation on separate bounded attempts.
- **At this earlier checkpoint, two implementation seams were active:** provenance
  was adding validated correlation-safe output manifest/artifact/attempt/operation/
  Job-UID projection, including late/quarantined/cancelled/unknown states; A2A was
  adding content-free message/result/cancel outbox events and ordinary peer drain.
  The later schema-52 receipt below supersedes this source-gap status. Its full
  current-source gate remains pending.
- **Real-reasoning admission remains unapproved/unperformed.** The precise
  requested profile is a separately guarded local process/workspace that reuses
  the owner's shared account database without copying OAuth material and never
  attaches the general coding service. Require deny-by-default permissions with
  only the explicit MCP tool whitelist, an operator-approved model, at most three
  steps / 120 seconds / 8192 observed tokens, and included ChatGPT OAuth only
  (no API-key or paid-credit fallback). The token threshold is an observed-usage
  fence, not a hard pre-token ceiling. No profile has been admitted. A named
  independent host is a separate user decision; local reasoning authorization
  would not prove laptop-off operation. See
  [`remote-reasoning-runtime.md`](remote-reasoning-runtime.md).

### 2026-10-04 — Earlier provenance/A2A outbox checkpoint (superseded)

- The provenance lane's local collector integration exercised 14 ordinary-peer
  records with no replay duplicates, sender/receiver attribution and message/result
  causation, plus revoked-proof retention (3 pending, 0 acknowledged at frozen
  epoch). The final acceptance assertion still fails: shared-client optional null
  reference keys do not match collector responses that omit those keys, so accepted
  delivery acknowledgements remain zero. This is a concrete remaining source seam,
  not a pass.
- The A2A lane reports **35 SDK/store/bridge/outbox tests passed in 8.50s**. A new
  two-process installed-peer/collector run passed discovery, submit/result/replay,
  changed-key denial and cancellation, with causal outboxes surviving replay, but
  stopped at the final acknowledgement assertion. Reference artifacts remain
  `verified:false`; all child servers were stopped, and no inference ran. Do not
  call this ordinary outbox acceptance until the acknowledgement contract converges.
- Both provenance and A2A source lanes remain active; final Python/install gate
  waits for their repairs. The root's 22:39 UTC quota refresh reported healthy
  telemetry at about **30% remaining**, not exhaustion. No paid inference fallback
  or unapproved spend was used.

### 2026-10-05 — Schema-52 fresh-namespace normal installed success

- Latest sanitized receipt:
  Local record ID `REMOTE-PLATFORM-FRESH-NORMAL-2026-10-04`
  (schema 52; installed image
  `sha256:ff90ef05b7ba771c2ee400b3f475e29eeda48cc84a74fe2b61583eb5a2f2b950`,
  wheel SHA256 `d982601e437a0858af1a3572d9e0cd33dd763e40c387c609cd588babd17fd33f`).
  An explicit schema-52 migration and control rollout passed; the owner reports 24
  focused migration tests. Fresh local namespace
  `tessera-platform-workloads-fresh` UID `abbba7c3-ddf9-4ce2-9f1c-e4a756ece1f8`
  has quota 3 Jobs/8 Pods/1Gi and a separately isolated same-tenant project
  (concurrency one, zero exposure). Old six-Job bench/quota was unchanged; prior
  run-5 cancellation and run-8 quota-403/unknown accounting remain preserved.
  No SQL cleanup or concurrency widening occurred.
- Normal installed attempt passed **all 16 configured journey stages**. The
  ordinary Job UID `fbfc603e-9d6a-4a41-a0ad-1ffb51c778fb`, operation
  `9fb39348944202be13192dbd93699585`, attempt
  `experiment_admitted_attempt_8635fa71bfde44c1b71496331c8e322d` and output
  manifest `artifact_manifest_c8ab0f2c805cfbf8446edf1a517b7806` are correlated.
  Manifest was recorded on time (`late=false`); supported control outcome resolved
  **`succeeded` (HTTP 200)**. Actual human output-proof issuance returned **201**
  and exact same-key replay passed. Readback matched 36 bytes, public value 385,
  SHA256 `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`.
  Human disposition remains `retain_quarantined`, custody was released, and
  scientific validity is **not assessed**. This distinct success does not alter or
  erase the prior run-5 late/cancelled artifact.
- MCP proposal/authority/admit/package/dispatch/status/output/replay, expansion
  denial, post-success restart generation, separate expiry/revocation fences,
  observation/review and joined export passed in the receipt. A separate MCP
  cancel was pre-submission (no lease/package/Job), not termination of a running
  Job. Two independently installed reference peers each drained four causal
  metadata events with exact replay and zero pending; eight records joined the
  notebook. Critic output remains reference-only; **no reasoning inference** was
  used. Final one-page notebook readback at sequence 112/event cursor 11983 joins
  output manifest/artifact/hash/attempt/operation/validated Job UID plus peers.
  Coverage for historical uninstrumented/off-platform work remains unknown.
- Focused supporting evidence: 58 provenance tests plus a human-authenticated HTTP
  case; 78 client-normalization tests; 24 schema-52 migration tests; installed
  isolated two-peer delivery/readback with 14 acknowledgements, zero pending,
  replay without duplicates; a revoked task's three events remain pending at its
  frozen epoch. These are bounded component/integration results, not the final
  current-source full gate.
- The schema-52 checkpoint confirmed its output-correlation and reference-peer
  acknowledgement seams for that bounded local journey. The former blanket
  “no source implementation gaps” conclusion is withdrawn by the revision-3
  source inspection below, which identified further consumer, output-registration
  and portable-runtime work. This did not mean every platform acceptance check
  passed: the receipt explicitly sets
  `reasoning_verified`, `independent_host_verified`, `laptop_off_verified` and
  `integrated_acceptance_verified` false. The fresh local installed scenario is
  evidence for its configured boundaries only.
- The final current-source Ruff + four-shard gate was **not collected**. After the
  earlier pause, root and parent resumed the same journey child
  `ses_ef8b39b5effeB4gZR6s7tenGiF` solely to collect its existing gate; that
  admission was again denied with `misalignment_policy_violation` /
  `Potentially unintended activity`. The child is now paused with no active
  descendants. No gate was collected or repeated, and no service/source change
  followed this denial. This is an actual safety-admission blocker, not telemetry
  exhaustion, a test failure, or a pass. Root must resolve legitimate admission in
  that same child before retrieving the existing gate; do not use another worker,
  model, or bypass. Root and parent quota telemetry at 00:28 and 00:29 UTC was
  healthy at 23% remaining, not exhausted. The old 3454-pass/1-skip receipt at
  digest `8973...` is historical and does not cover current code.

#### Current acceptance matrix (intent revisions 3–8; source/test slice is not live acceptance)

| Intent check / user journey | Owner lane / components | Local proof recorded | Live proof / external prerequisites still required |
|---|---|---|---|
| `authority` — issue bounded grant, consume for typed proposal/dispatch/cancel/delegation | Authority-backend and runtime-binding owners; tenant-scoped grant/store/API/native owner | V7 local component evidence includes backend 32, migration fixture 18, API/authority 31 and paired native 25 distinct cases with composite—not single-run—coverage. Actual bounded same-grant WS handshake/frame, independent nonce/tickets, readback/exhaustion denial, last-unit reservation and uncertainty/restart fences are documented in the paired receipt. Upstream owner/OAuth/quota facts are synthetic; no real grant effect. Run-8 quota/accounting unknown retained. | Exact-current full source gate and installed operator flow with genuine human-issued grant, tenant/runtime and approved host remain required. No grant widening or agent self-approval. |
| `runtime-admission-contract` — enforce versioned, evidence-backed local readiness without treating provider-unknown or operator assertions as fabricated provider proof | V6 contract plus v7 Python Store/API/verifier and isolated native HTTP/WS owner bridge | Decision 0029/v2 preserve strict v1 default, explicit v2 opt-in, and deny unknown required local checks. V7 actual source path now exercises producer/private guard and quota predicate, authenticated Unix proof IPC, HTTPS API, Python verifier before claim, canonical SQLite claim/writer fence through child exit, actual SDK HTTP/WS lowering and private native lock/settlement. Native 79-source cold SDK HTTP/WS and Python import gates pass. Upstream identity, operator, OAuth/quota and primitive resources remain synthetic. Exact receipts below. | Genuine approved principal/session/publisher/issuer enrollment, secure owner resource handles, authenticated financial reference/revocation/profile-age resolver, production guard/quota, installed full SDK/config/skill/catalog/toolmap and deployed host remain unverified. No live provider or production admission. |
| `notebook` — approved interactions and generated outputs become redacted durable provenance | Agent-provenance lane; public capture/outbox, schema-53 registry, joins/projection | Prior guard-provenance evidence remains bounded local source proof. Intent-8 starter/native handoff additionally links one actual ordinary Job's 1,318-byte quarantined result and measurement to an admitted local experiment, then records separate human review/custody release and byte-exact HTTPS export. Synthetic temporary human/worker identities; no generated planner/critic messages. | Full public-harness message/permission/delegation notebook join, real generated reasoning and production identities remain unverified. Native artifact/measurement linkage is not the complete planner→critic→human-review journey; no raw prompts/headers. |
| `remote-runtime` — planner/critic generated research, persisted and resumed in same session | Reasoning-runtime lane; isolated public OpenCode snapshot, SDK publisher/auth adapter, default consumer/factory | Isolated runtime source SHA `6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`; reported 52 Bun +4 Python tests, two reproducible isolated builds and zero dispatch. The later exact wheel SHA `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99` has installed-origin and protocol receipt evidence for that artifact only. | Synthetic principal/resources and protocol outputs only; no actual included-OAuth research session, real inference, generated planner/critic reasoning, experiment, same-run restart or running-job cancellation. Subsequent source/test changes supersede that wheel as current candidate. |
| `full-mcp` — discover approved catalog/skill and perform typed propose→run→observe→review/export | Full-MCP lane; consumer factory diagnostics, guard join, SDK binding and stdio service | Factory diagnostic seam: 11 tests passed in 5.48s, 3 existing warnings, source SHA `97311053d3056ec11c79ffe27aa06a8da96ce5bc19a60dcba453c849303ff3dd`. Final-installed wheel compatibility receipt passes one test/eight stages/29.61s against wheel `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99`; installed 248 product files/136 imports checked, actual MCP stdio catalog (18 tools), scoped operations, reconnect and replay. | Exact receipt binds only the retained wheel and preceding source snapshot. No actual research harness discovery, authenticated principal, experiment/review/export or admitted runtime. Do not claim full workflow or HTTP MCP. |
| `a2a` — independent critic artifact/reference is consumed by planner peer | A2A lane; real local MCP stdio service plus independent A2A process harness | Final-installed compatibility record ID `LOCAL-PROTOCOL-V5-FINAL` passes one test/eight stages/29.61s/native exit 0 against wheel `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99`; two independent peers, restart/replay/cancel and five collector receipts. | Synthetic critic bytes/model/source and human fixtures; no inference, approved principals, experiment or human review. Peer `-15` exits are owned cleanup; MCP child exits unknown. No off-host/laptop-off proof. Later test/doc changes supersede the artifact as current source. |
| `robustness` — withdrawn-context, owner-fenced Pod pagination, atomic desktop save | Worker/desktop lane; source and regression tests | Prior targeted local regressions retained. Revision-5 exact-source gate runner covers artifact-inventory safety, not new product robustness behavior. | Reproducible local regressions plus real external-cluster pagination and physical native-picker/device proof where applicable. No claim from unit tests alone. |
| `integrated-journey` — planner→generated critic→human approval→experiment→notebook; restart/cancel; hybrid then independent host | Platform-convergence lane; runtime, MCP/A2A, authority, provenance, operator and Docker profile | Schema-52 16-stage journey/eight ACKs are historical. Intent-8 provides a separate actual local native baseline Job: fresh allowed/denied network evidence, pinned OCI manifest, leased snapshot bytes through durable UID-bound ConfigMaps, quarantine, experiment measurement link, human review and export. This was one baseline, not the four-trial Docker sweep. The later conservative custody hardening means the native run is not final-source proof. | Generated planner/critic and human approval journey, active-Job restart/cancel, final-candidate-bound native rerun, production identity/provider, garage/off-host execution and physical laptop-off remain open. |
| `observability-slice` — final hook phase→public SDK channel→trusted observer join | Reasoning-runtime and provenance lanes; public hook/SDK, publisher, Tessera join and factory | 21-case SDK channel receipt plus 11 factory tests and 102 provenance tests (separate receipts, not added); source revisions above. Actual source publisher/SDK read-consume and temporary durable backend executed locally; synthetic authority/credential/guard facts, zero dispatch/network. | No production account/owner guard, genuine principal, installed authenticated channel, final provider transport or admission. Operator financial state and provider entitlement/billing remain separate/unknown. |
| `release-candidate` — reproducible package/source candidate, clean install and exact-revision gates | Packaging lane; isolated runtime bundle, lock repair, protocol compatibility, exact-source gate runner | V5 source `3bd2d1a850951fa23d64a23ff8b9cbd1fb25deed3460a82d550c0884ee799145` / wheel `fc537c837d3543656add8e696ff468615449b4f88479246bea2a60da6a649eea` retain the 4,503-case/Ruff gate. Intent-8 packaging phase 2 integrates the closed 14-file runtime-v8 bundle; phase 3 adds six exact tabular example resources and installed-resource checks, with 31 and 9 distinct scoped checks respectively across retained runs. Proposed `0.1.0rc2` remains unbuilt. | All source/package owners now report quiescent. The old 792-input pending list is superseded, not a freeze. Root must now create the final complete input inventory/digest, reconcile the legacy v35 fixture handoff, freeze and build the proposed candidate, then run exact-candidate distribution/install, six cold-source commands, whole-current-source gate, and installed MCP/A2A rebind. Do not relabel historical v5/v5-installed evidence. |
| `journal-install` — durable decisions, exact receipts and operator/agent install workflow | Platform-journal lane; canonical ledger/docs | Intent-8 source checkpoints include the authenticated dataset API/materializer (28 distinct API cases composite), runtime setup (22 local tests composite, synthetic private-owner resources), runtime-v8 package integration (31 scoped checks), six starter package resources (9 checks), and actual local native baseline/review/export handoffs. Evidence counts are separate/composite, not one suite. | Root owns final required-doc reconciliation, candidate inventory/freeze/build/install and exact-source gates. Native execution was actual local Docker Desktop Kubernetes with synthetic principals; final-source candidate-bound native rerun, production admission, generated reasoning, device and off-host acceptance remain separate. Schema-55 source and schema-52 installed evidence remain distinct. |

The rows above summarize the current matrix; [`IMPLEMENTATION_TASKS.md`](../IMPLEMENTATION_TASKS.md)
tracks the same status and links back here. Lanes are disjoint; source work is not
evidence that an end-to-end journey passed.

### Intent revision 8 — dataset API, native starter and package integration; current wheel needs native-response repair

- The dataset owner completed the normal authenticated API and worker materializer
  path. Human-authenticated snapshot adoption stores the actual bounded CSV bytes
  and observed manifest in the existing snapshot ledger; adoption alone does not
  release data. Separate human release, approved grant/task, worker audience lease,
  exact-byte materialization and same-bytes loader checks ran through actual FastAPI
  handlers and SQLite using temporary principals and synthetic CSV. The owner reports
  28 **distinct composite** scoped cases across retained selections, not one 28-case
  invocation. Final source inventory: `c29ab127db81fb24361d51f2e71d5eeea6617e1770d5514b3edd46a55dd78e0c`;
  local evidence ID: `DATASET-API-INTENT8`.
  This API/materializer lane alone did not run a native Job. The later native owner
  completed a separate one-baseline Job journey recorded below; it is not the four-
  trial Docker starter sweep.
- The runtime setup owner completed source/API and reconciled the ordinary operator
  guides. The handoff records 22 distinct passing tests composite, including actual
  temporary HTTPS enrollment, immutable financial issuance/reference resolution,
  audience checks, revocation/expiry/readback-only denials and a Python module
  invocation from a different working directory. Owner signature/process/private
  resources remain synthetic. Exact source inventory namespace:
  `89870e47bc010ea425abb7685f571b7571c2aa0631d996dc169656365bb17ccc`;
  local evidence ID `RUNTIME-SETUP-INTENT8`. These source helpers do
  not establish genuine production owner resources or installed admission.
- The isolated public runtime-v8 source deliverable is now shipped in
  `release/opencode-observer/runtime-v8/`: closed v3 manifest, 14 public files,
  50 pinned originals and 79 target files, both actual MIT license bytes, locked
  public dependencies and cold reconstruction/link/verifier commands. Bundle
  initial namespace `5c5e4ec4d6e1428575e205aaac0e9144f3528c9923d396147c390c0d277cd8f0`;
  79-target namespace `c7c70b367eff949c5141979b5de4047f09892d4fdf15573ce9137c5e36fd8062`.
  The root package phase integrates exactly these 14 files and validates the closed
  schema/staging/inventory boundary. Public-source reconstruction and SDK checks are
  not a full upstream OpenCode build, installed final-wheel check, upstream commit
  verification or live provider proof. Handoffs:
  local evidence IDs `OBSERVER-RUNTIME-V8` and `RC2-PACKAGING-PHASE2`.
- The earlier package phase-2 pending-list SHA
  `6ce9689f91226115b738e4a39e28ca67ae03ac5d27643f446c8d0b1b37bcd0a0` is superseded;
  it is neither the final root inventory nor a freeze. A first `0.1.0rc2` candidate
  was subsequently built twice and failed the required source lint; details are below.
  The helper repair and package-pin updates are new source inputs, so that first
  candidate remains immutable historical evidence and cannot be relabelled as the
  repaired source. Freeze/build the next attempt only after those inputs are pinned
  and all writers are quiescent.
- Starter handoff: the actual OCI manifest `sha256:1881046340e29da4ce2d60a71d0b05232c7fb69ae64126317b4ffdd49a135cde`
  ran four preregistered sequential trials in a local Docker API journey; its one
  exact-image gate passed. A separate actual local native Kubernetes journey ran one
  baseline Job with fresh allowed/denied network evidence, leased snapshot bytes,
  durable UID-bound input resources, bounded init verification, quarantined output,
  experiment measurement link, separate human disposition and byte-exact export.
  The native run's initial harness exit 1 was due to a response-field mismatch and
  namespace wait timeout after the Job succeeded; same-state review resumed without
  resubmitting a Job and exited 0. Exact result was 1,318 bytes with SHA-256
  `3ef842bec9d877fbcfeec4b554b6f0d73ce2f04519411c4ccba61433ff638686`.
  Temporary principals and generated data are synthetic; this is local executable
  Docker/Kubernetes evidence, not scientific validity or production identity.
  Final conservative custody hardening occurred after the native run, so that run is
  not final-source proof. Local evidence IDs: `TABULAR-STARTER-INTENT8` and
  `TABULAR-NATIVE-INTENT8`.
- Still separate and pending: clean final wheel install and installed-resource check,
  six exact public cold-source commands, whole current-source gate, installed MCP/A2A
  protocol rebind, candidate-bound native rerun, production admission with genuine
  owner-held resources, generated planner/critic reasoning and human review,
  active-job restart/cancel, garage/off-host execution and physical laptop-off
  evidence. Do not substitute the real local starter/native run for these checks.

### First intent-8 `0.1.0rc2` candidate attempt — immutable, source gate failed

- The first frozen candidate used root-source SHA
  `6242edabe03b566199eb34993b79b7fce3efd8a7eb0ad41b98d3af939022394a` (799 builder
  inputs; 849 root inventory entries). Two real builds produced byte-identical
  source archives and wheels: source archive SHA-256
  `e39f9cc231d6013bf1b03e8a8bcf29ce8053317874c61dce4bcb6b93401f6e7f`, wheel
  SHA-256 `ab8cfd46cee3a313973e52d5e408c0de7a2ff993ab04d93e228704676219fd26`.
  This was a measured candidate attempt, not an accepted release. Full handoff and
  retained evidence ID `RC2-CANDIDATE-UGUFLN96`.
- The actual clean owned environment installed the candidate and locked runtime/MCP/
  A2A distributions; installed code/resource origins, schema-55 fresh initialization,
  five console helps and three module helps passed. Four installed migration/rollback/
  backup/held-restore cases passed. The candidate-bound public cold checks passed
  their six required boundaries: Bun selected tests **16 passed** and Python client
  tests **8 passed**. Do not merge the prior 17-case composite with this distinct
  16-case invocation. These results bind only to the first wheel/source digest.
- The required source gate then failed: `.venv/bin/ruff check .` exited 1 with 15
  findings (12 E501 and 3 I001) in three shipped runtime-v8 helper files. Ruff format
  and full pytest were **not run** after that first failure. The complete failure
  receipt is retained in the candidate handoff. This `rc2` attempt is not a passing
  release candidate despite its reproducible builds, installs and bounded passing
  package/cold checks.
- The isolated helper repair produced new helper bytes and a new 14-file bundle
  namespace `5ee39d0311d8a4b4f0f59298f7591bd8849e772ac69ba629dd3ff4e264c800b7`;
  updated `baseline.json` raw SHA-256 is
  `45d2645a5bb29a4b2ebe771eb0c9b8802110e4a00423f7baf844d0e10ed9f9a5`. The 79-target
  source namespace remains `c7c70b367eff949c5141979b5de4047f09892d4fdf15573ce9137c5e36fd8062`.
  The first bundle namespace, candidate and gate receipts remain untouched. At this
  checkpoint the package owner's bundle pins/validator tests still needed update;
  the later reviewed repin and new candidate are recorded below. The earlier
  16-Bun/8-Python results do not verify the repaired helper bytes until their exact
  wheel-byte identity is separately established.

### Intent-8 follow-up — source candidate and native response-cap repair

- After the helper-style repair and reviewed bundle repin, a distinct source freeze
  was built twice reproducibly as `0.1.0rc2`. Frozen root-source SHA
  `245972b82bf374fc869ba76d877ee8ed068fb4c1ffec24ef308b3ae3398998de`; source archive
  SHA-256 `668a38698615ea6ddb0fc9fbc0c62c5fb967295250a0cc2129916d4152dbe4a5`; wheel
  SHA-256 `9aa50dd7383ae7489b08d7b153298edfabc35dbc0935b9214e50e660182336bb`. Both
  second-build artifacts were byte-identical. The public bundle repin is
  `5ee39d0311d8a4b4f0f59298f7591bd8849e772ac69ba629dd3ff4e264c800b7`; 79-target
  namespace stayed `c7c70b367eff949c5141979b5de4047f09892d4fdf15573ce9137c5e36fd8062`.
  This candidate supersedes the failed earlier `e39f9c`/`ab8cfd` attempt as the
  latest measured package, but not the subsequently repaired native-client source.
  Local evidence ID: `RC2-CANDIDATE-KML903EK`.
- Exact-source standard gates passed on that frozen source: Ruff check and format
  both exit 0; the whole collection and four sanctioned shards formed a disjoint
  complete set of **4,898 passed, 47 skipped, 66 warnings**, no failures/errors.
  The 47 skips are explicit opt-in prerequisites (25 native-binding cases and 22
  terminal-inventory cases), not waived passes. Fixture-only migrated-v35 repair
  preserved its separate five-case receipt; it changed no production source.
- Exact wheel-byte identity was checked against the earlier candidate wheel, so its
  installed byte/origin/migration and six public cold-command receipts (16 Bun and
  8 Python selected tests) were explicitly reused, not rerun or relabelled. A separate
  installed protocol run passed one case/eight stages (receipt SHA-256
  `4d0812c826810ad0f8be8f0802daa647b834c7616867067faff4d6709876dc63`). It exercised
  real MCP stdio and two installed A2A peers, but generated text, identities and
  admission resources were synthetic; it is not generated reasoning or full research
  acceptance. MCP SDK child exit codes remain unknown.
- The installed native attempt used the exact `9aa50dd…` wheel and current-source
  root inventory. The observed probes returned allowed HTTP 200 and denied timeout;
  this differential observation was not causal proof of policy enforcement. The
  normal API created synthetic human/worker roles, released data and established
  plans/grants/lease. It failed during preparation before any Job-create callback:
  zero Jobs, no termination confirmation, and restart/cancel/measurement/quarantine/
  review/export stages were not run. Six ConfigMaps were planned; the manifest UID
  was persisted before a chunk response exceeded the installed client's 64 KiB
  output cap (planned chunk response 874,630 bytes). The cap diagnosis is inferred
  from installed code, response size and custody journal—the original scrubbed native
  exception text was not retained. Local evidence ID: `TABULAR-NATIVE-9AA-ATTEMPT`.
- The narrow repair changed only
  `src/tessera_worker/docker_desktop_kubernetes.py` (SHA-256
  `f68fdf70b42c5693c1bf8f37e12ba4b322ae7463d11be17cbe5826b1e3fae488`) and added
  `tests/test_kubernetes_dataset_responses.py` (SHA-256
  `16106a8ff513d55dbd17003e31ff4c3b063d2f4e03fa06029db33b63004dfb49`). Dataset
  ConfigMap create/read now have a scoped <=1 MiB response path; generic 64 KiB
  conformance cap and existing authority/cluster/namespace/RBAC/network/PVC/UID
  fences remain unchanged. The owner reports 27 targeted tests passed in 2.37s,
  Ruff/format/diff checks exit 0; subprocess responses were isolated fixtures, not
  another live cluster run. Local evidence ID: `DATASET-RESPONSE-FIX-INTENT8`.
- That client repair changes source after the `245972…` freeze, so the `668a386…`
  archive, `9aa50dd…` wheel, 4,898-case source gate and native preflight are historical
  evidence for their exact revisions. The next candidate requires a fresh inventory,
  freeze, reproducible build/install and affected source/protocol/native gates. In
  particular, a new installed baseline Job followed by active-process restart/cancel
  has not yet passed. Genuine production identity/provider facts, model-generated
  planner/critic reasoning, human research review, off-host/device/laptop-off remain
  unverified. Do not edit shipped inputs to narrate future gates after the next freeze.
- The 47 opt-in runtime-binding/admission cases skipped by the root source gate
  received separate current-intent coverage: 22 interop cases passed composite across
  two retained selections; 25 paired cases passed composite across 14 retained cases
  plus 11 affected reruns, with no skips in the final rerun. These are distinct
  coverage sets, not one 47-case run or production-owner proof; upstream identity,
  quota and private resources were synthetic. The root-gate skip count remains as
  recorded and must not be converted into 47 passes in that source-gate receipt.

### Earlier `0.1.0rc2` candidate `c575` — dataset-response fix; superseded by `6ceb`

- A fresh source freeze `a6007bf1275c5679d87de02d9b0ee1e22d1863c8f668e0ec792a44b044a96e4f`
  built twice byte-identically: archive SHA-256
  `e6fc7a2cf119595cc89e6fb364a6d525e26baf201834a86c026c9b32db16ba28`, wheel SHA-256
  `c57556ba9263e5a189469ab85c125d738e82d8cee966f2be3cac19fc8b8095b5`.
  The fresh wheel install and schema-55 initialization passed; four installed
  migration/rollback/repeat/backup/held-restore cases passed. The installed public
  bundle required checks passed (16 Bun, 8 Python). Full handoff:
  local evidence ID `RC2-CANDIDATE-LRP7GDB9`.
- On that frozen source, Ruff lint/format passed and the 4-shard full collection
  completed **4,912 passed, 47 skipped, 66 warnings**, no failures/errors. The 47
  skips were the 25 native-binding and 22 terminal-inventory opt-in cases; their
  later separate composite receipts are described above, not folded into this count.
  Afterward only `tests/test_runtime_binding_e2e.py` was changed to bind the tests to
  current intent-8 baseline/input pins (SHA-256
  `3298e0345e1f5a4834fd70dd513a7764f81a4236cdfd0aab969ce722dcf1cf96`). Therefore
  the 4,912-case gate applies to its frozen inputs, not the subsequently modified
  source tree; include that test-only change in the next candidate and gate inventory.
- Fresh installed MCP/A2A compatibility against the exact `c575…` wheel passed one
  case/eight stages (receipt SHA-256
  `270f2e92a4af680b4df964e6e824ad9515161bb9ec273f78b4ac5ddcacb01351`). The two
  independently running peers and MCP stdio exercised the described protocol stages,
  but generated outputs, identities and admission resources were synthetic. It is
  not genuine planner/critic reasoning, production enrollment or full research
  acceptance; MCP SDK shutdown exit status remains unknown.
- The `c575…` installed native attempt **failed admission before any Job**. Both the
  allowed and denied client probes returned HTTP 200 to the same reachable endpoint;
  enforcement was recorded failed. No control grant, Job, restart/cancel, measurement,
  quarantine, review or export stage ran. Exact candidate-bound handoff:
  local evidence ID `TABULAR-NATIVE-C575-ATTEMPT`; exact-UID cleanup
  and subsequent NotFound were confirmed, with no unrelated cluster change.
- The earlier read-only diagnosis that kindnet had no observed enforcer is superseded
  as a current-target conclusion by the one specifically authorized restart and fresh
  causal probe below. Its inspection was read-only and did not prove universal
  absence. The `9aa…` timeout remains differential history only; its policy causation
  is still unknown.
- Dataset-specific bounded-response repair is present in the `c575…` wheel; its 27
  regression checks used isolated subprocess fixtures, not a live cluster. The
  test-only admission fixture update also landed after the `c575…` freeze. A subsequent
  serial preparation/lease-renewal repair is recorded below and changes four production
  modules plus four tests. The `c575…` install and 4,912-case source gate do not cover
  that repair; rebuild/install and affected exact-source gates are required.
- Admission-owner receipts report 22 interop and 25 paired distinct local cases
  passing composite with synthetic upstream/private facts. They do not establish a
  genuine signed owner manifest, real private runtime resources, provider entitlement
  or actual provider request. Live generated reasoning, full human research review,
  independent-host/device/laptop-off acceptance remain unverified. Candidate build
  and passing component receipts do not close those journeys.

### Intent-9 approved kindnet restart and native preparation repair — no Job yet

- The user explicitly approved exactly one restart of the existing
  `kube-system`-owned `kindnet` daemonset using context `docker-desktop`, including
  transient impact to other cluster workloads. One restart and rollout completed
  (native exits 0); daemonset UID and image were unchanged and the new Pod UID was
  `2937804a-bb6f-470f-abd8-6f8c24d2e4e8`. Read-only before/after snapshots observed
  68 unrelated Pod identities/statuses unchanged; this is not proof of continuous
  availability. No second restart, CNI install, network-config change, or cleanup of
  unrelated work occurred. Receipt:
  local evidence ID `INTENT9-RESTART-CAUSAL-NATIVE`.
- After that restart, the same causal client Pod/source IP and same healthy reachable
  server/Pod IP produced **deny timeout → allowed HTTP 200/body → deny timeout** on
  fresh source ports. An independently allowed client returned HTTP 200 in every
  phase. Actual policy/resource UIDs, routes, source IP and NFQUEUE/controller evidence
  were retained. This establishes causal network enforcement for that bounded local
  probe; it corrects the prior diagnosis for the restarted target and does not convert
  the earlier `9aa…` timeout into proof. The owned probe namespace was exact-UID
  deleted and absence verified. No further kindnet restart is authorized.
- The subsequent native attempt used the exact installed `6ceb…` candidate after
  those witnesses, but ordinary preparation still failed before any Kubernetes Job
  create callback. The synchronous preparation path consumed the initial 30-second
  lease before its renewal loop ran; no renewal was accepted before expiry. No Job UID,
  workload execution, review/export, measurement, process restart or active-grant
  cancellation was observed. The synthetic grant and held 5-cent reservation remain
  untouched: no refund, reopen, replacement operation or retry. Original max-3 Job
  budget remains 0 used. Full result:
  local evidence ID `INTENT9-RESTART-CAUSAL-NATIVE`.
- A bounded source repair now serializes preparation commands, validates all planned
  resources before effects, records each UID before the next command, and renews on
  the supervisor thread between commands under fresh grant/input/epoch checks and the
  existing 30-second callback cap. The hard lease bound and uncertainty/no-replacement
  behavior remain. Four production modules and four tests changed; exact eight-file
  source digest `53ceef5e290b386dcc416a841b70ef0d42a103daf597cf1c50d3f489a7a4b28d`.
  The owner reports 151 distinct scoped cases composite (150 regression cases plus
  the corrected execution expectation), plus a separate 16-case custody selection;
  these are not one 167-case run. Ruff, format and diff checks passed. Test timings and
  upstream renewal responses were synthetic; there were no native Jobs after repair.
  Local evidence ID: `INTENT9-PREPARATION-RENEWAL-REPAIR`.
- Immediately before the intent-9 native attempt, a later actual `0.1.0rc2` candidate
  was built twice reproducibly: root namespace SHA-256
  `5bc0dde0526aa46e62aa44399555c832b14ebbe64d3c5a41e886b96382fa21af`, source archive
  `1933fc88acf6bf0b4fca6727ce07b8b31696a50f0820f3f91b092130cf7cb82c`, and wheel
  `6ceb1e04f56e968f98b5b892d263a84493f0be9380b0dba2af1a9776f5066170`. Its fresh
  installed migration checks passed 4 cases (3 warnings); installed-origin checks
  passed. Ruff/format/diff checks passed and its disjoint whole collection plus four
  shards yielded **4,912 passed, 47 skipped, 68 warnings**, no failures/errors. The
  separate 22 interop + 25 paired composite receipts remain distinct and are not
  recast as a single 47-case run. Exact packaging handoff:
  local evidence ID `RC2-CANDIDATE-ZP3L9N3`. The earlier
  16-Bun/8-Python and eight-stage installed-protocol evidence was reused only as
  unchanged-component proof from `c575`, not rerun for `6ceb`.
- The `6ceb…` wheel/source and its native preparation failure remain immutable
  revision-bound history. The later preparation repair invalidates affected
  production-module, installed-origin, source and native gates. A new complete source inventory, build/install,
  current-source gates and then fresh native baseline/review/export plus same-Job
  restart and active-grant-revoke cancellation checks remain. The causal network
  witnesses are historical proof for their exact target state; do not claim a Job ran
  or replay the old synthetic grant. Genuine production owner/provider binding,
  generated research, off-host, device and laptop-off acceptance remain separate.

### Current frozen source-gate receipt and preceding failed run

- The latest fresh frozen convergence gate passed on selected snapshot digest
  `b3677c4539c777bb90b6f64a939daf9a6ad6fdcaab22afd45175b5f3805b0c38`:
  **4,268 cases passed; 0 failures, errors, or skips**. Four shards reported
  907/1,263/663/1,435 passes. All six native commands exited 0; Ruff 0.16.6 check
  and format both exited 0 (677 files already formatted). There were 14 warnings in
  four categories; wall time 2,006.651 seconds. The frozen 713-file/8,997,900-byte
  selected snapshot matched before/after and the workspace was aligned at gate time;
  no owned process remained. Receipt and handback:
  local record IDs `SOURCE54-CONVERGENCE-RECEIPT` and `SOURCE54-CONVERGENCE-HANDBACK`.
- Preserve the earlier current-source standard-gate failure unchanged:
  local record ID `SOURCE54-STANDARD-GATE-FAILURE` recorded 4,223
  passed and 6 failed. Its named repair trail is retained exactly: facade 21;
  rows 2–4; native DTO 9; derived fixture 3; guardian bounded-readiness 3 plus 2
  concurrent cases; README format 0. These are affected row/test scopes, overlap,
  and are not arithmetic additions to the final 4,268. The historical
  3,454-pass/1-skip baseline is older still and remains a separate receipt, not the
  current gate.
- The gate includes the partial trusted-observer producer and `research_identity`
  source changes. The separate identity library receipt reports 17 focused tests
  passing: actual owned-child PID/parent/start, OS executable path/hash/size,
  installed METADATA version and artifact export; proof is nonce/30-second-bound
  and explicitly `complete:false`. It does not establish an OpenCode process or
  inference. A source producer/RPC fixture reports 22 focused cases for authenticated
  RPC, nonce/TTL, and available transformed-tool/session/catalog/skill-hash/
  permission/provider/session-version/connection/transport-origin fields; it also
  returns `complete:false`, not admission or a truthful complete measurement.
- The observer plugin/Python producer extension uses the same authenticated V2 RPC
  registration; it is not an existing generic OpenCode telemetry API. It reads only
  supported fields and reports allowed HTTPS/WSS origins without auth headers,
  bodies, frames, paths or queries. `allowedTransportOrigins` is a human constraint,
  not measured authorization/billing. The current public API exposes point-in-hook
  observations, not final hook ordering or immutable post-hook request. The exact
  missing measurements and minimal handoff are documented in
  [`plugins/tessera-harness-observer/README.md`](../plugins/tessera-harness-observer/README.md):
  effective/final policy/config, complete discovered catalog, selected raw skill
  load, supervised Tessera build/child SDK linkage, public included-OAuth
  entitlement/billing, final request-hook chain/post-hook drift, and dedicated
  trusted process/origin evidence. `ProviderContext.Provenance` is historical route
  provenance, not entitlement or a final seal. A valid signature authenticates an
  issuer, not truth. The `/tessera/runtime-guard/v1/observe` route remains a Tessera
  contract, not an existing OpenCode endpoint.
- A bounded external metadata extension would require authorized writes outside
  Tessera, specifically OpenCode's `packages/core/src/plugin/hooks.ts`,
  `packages/core/src/session/model-request.ts`, a typed metadata schema beside
  `packages/schema/src/session-provider-context.ts`, and protocol/SDK exposure.
  No such external source write, global config/policy change, running harness
  mutation, OAuth transfer or API-key fallback was authorized or performed. Root's
  next decision is to request that narrow extension authorization or supply a
  trusted independently measured handoff; the partial producer does not count as
  complete source/admission or live reasoning.
- After this frozen receipt, the v4 Tessera contract added three files and its own
  37-case focused receipt; the full aggregate was not rerun. The passing 4,268
  receipt still applies only to its exact pre-v4 snapshot. Journal/backlog edits
  after the v4 source change are docs-only and were not retested.

### Intent revision 4 — bounded model-hook phase slice; full observability still open

- **Tessera-owned local contract completed:** `src/tessera_mcp/guard_phase_evidence.py`,
  `tests/test_guard_phase_evidence.py`, and `docs/guard-phase-evidence.md` are the
  three bounded changed files. Module SHA-256 is
  `e644b355fb4bd0137f4d17e9b0d249d8609f6b1775ad255c4e6df073465fc9d7`. The assigned
  receipt reports 37 focused tests passing in 0.35s, exit 0, plus scoped Ruff lint/
  format and diff checks exit 0. It models typed non-admitting local guard outcomes,
  phase/order, nonce, session/proof references, fingerprints, 30-second freshness,
  strict unsafe-field rejection/redaction, late/order mismatch, operator-attested
  financial settings, and provider-unknown. It does **not** change or lower the
  complete-attestation admission verifier and does not test an actual core producer
  or enforced execution ordering. These fixture checks were reported by the owner;
  this journal worker did not rerun them.
- **Five-gap evidence map (root-published; status classes stay distinct):**

  | Producer claim | Current evidence class | Missing / not inferred |
  |---|---|---|
  | Final hook order and sealed post-hook request | Source-level point-in-hook origins only | Final order, mutation drift and immutable dispatched request are not measured or run. |
  | Secret-free final effective configuration | Source pins and strict receipt schema | Final merged/runtime config and effective permissions are not measured. |
  | Skill bytes and selected activation | Partial producer reports actual loaded skill-body hash | Raw selected bytes and activation/finality are not completely attested. |
  | Stable supervised identity/build/SDK | `research_identity` measures an innocuous owned child/OS/artifact; `complete:false` | No OpenCode process, installed Tessera build or launched SDK-child linkage is established. |
  | Provider route, entitlement and no-fallback | Public point metadata; operator financial attestation recorded separately | Actual account entitlement/billing and enforced final route/no-fallback remain provider-unknown. |

  The partial V2 producer still returns `complete:false`; the 22 producer fixture
  cases and the separate 17-case owned-identity receipt do not grant admission.
  Local-measured facts, not-run measurements, operator financial attestation and
  provider-unknown are not interchangeable or combined into a complete proof.
  Source limitations and proposed minimum metadata handoff are documented in
  [`plugins/tessera-harness-observer/README.md`](../plugins/tessera-harness-observer/README.md).
- **Workspace blocker resolved without policy override:** root approved the
  approved sparse checkout `OBSERVER-V4-SOURCE-SNAPSHOT` for the same runtime worker.
  The root `.tessera/` ignore rule already existed;
  there was no session move, alias, task-registry edit or active-control change.
  The isolated source is a 5 MiB selected public OpenCode 2.0.18 snapshot copied
  read-only from the existing public source tree. Its `BASELINE.sha256` pins exact
  bytes for five selected files; it has no Git metadata and no independently
  verified release checksum, so no upstream commit/release identity is claimed.
  No private guard, credentials, auth/config/environment files or dependency tree
  were copied.
- **Core phase producer implemented in that sparse checkout:** changed baseline
  files `packages/core/src/plugin/hooks.ts` and
  `packages/core/src/session/model-request.ts`; added
  `packages/core/src/plugin/model-phase.ts` and
  `packages/core/test/model-phase.test.ts`. The hook executor preserves Effect
  context, snapshots actual callback order, increments order revision on register/
  dispose, mints a one-shot sequence lease after model-request hooks, and rejects
  order drift, safe-projection mutation, replay, stale and future observations.
  An explicit supervisor challenge yields a frozen, safe, 30-second
  `opencode-model-hook-phase-v1` / `post-model-request-hooks` receipt bound to
  opaque references and the safe provider/model/tool-schema projection. Unsafe
  descriptions/defaults/examples/enums/headers/prompts/body/options/account/
  connection fields are excluded; required-field getters are not invoked.
- **Bounded core receipt (worker handoff, not rerun here):** Bun focused executor
  test reports 18 passed, 0 failed, 38 expectations, 321 ms, exit 0; Prettier 3.6.2
  passed on the four TypeScript files; isolated Bun syntax build of the two changed
  core entries passed with imports external. This is not full package typechecking,
  SDK/service preparation, complete SessionModelRequest execution, transport
  finality, or a live provider call. The isolated source artifact reported by the
  worker is SHA-256 `346bbd4629dc2acff0d8606d5d9c25f2fe2f26cf37498904c4d6665610875e64`.
- **Tessera observer integration remains conservative:** only the existing module,
  test and contract doc were changed in the Tessera repo
  (`src/tessera_mcp/guard_phase_evidence.py`,
  `tests/test_guard_phase_evidence.py`, `docs/guard-phase-evidence.md`); no
  nonexistent `model_hook_phase.py` is claimed. The integrated receipt SHA-256 is
  `8258be7009e79c20941f5f9489c524e4f49dd62cf5befc6e04ca09475f18c2a1`.
  The 57-case combined pytest result, Ruff and diff all report exit 0; it includes
  actual isolated Bun/Effect executor-to-Python consumption and rejects late order
  before serialization. No synthetic challenge was promoted to live evidence.
  Unsigned JSON does not establish authentication or live channel consumption; the
  parser requires an independent trusted context and its fixture is not an installed
  channel. `complete:false`, `phase_finality:unproven`, `ordering_enforced:false`
  remain explicit. Existing stricter admission is unchanged.
- **First-slice source artifact digest, reproducible selection:** SHA-256
  `e0db47b733dde1d5cc2bf60853a949fb9d635a761096a79aed8fc55f319f7b9e` over the
  ordered six code files (the four TypeScript files listed above, then the Tessera
  phase-evidence module and test). Canonical construction: for each UTF-8
  workspace-relative path in that order, hash `path + NUL + exact file bytes + NUL`
  into one SHA-256 stream. Docs, baseline manifest, runtime caches, dependency links,
  receipts, and all authentication/runtime state are excluded. This is a local
  artifact revision, not proof of public release identity or complete platform state.
- **Next implementation/verification slice:** expose the receipt through the
  supported public SDK/channel, then test actual service preparation and dispatch
  consumption (including final transport boundary). Separately obtain trusted,
  measured guard/quota and operator financial facts and bind them without defaults;
  prove final policy/config/catalog/skill/build/SDK and provider entitlement/no-
  fallback only at their actual boundaries. No claim of full observability or
  integrated acceptance follows from this first slice.
- **No live/active effects:** no provider inference, service restart/replacement,
  garage/Docker action, deployment retry, credential read/copy, environment-file
  edit, permission/financial-policy change, commit or full 4,268-case gate rerun.
  The frozen 4,268 receipt predates the v4 files; schema 52 remains the last installed
  evidence. This document update records the handoff; it does not independently
  rerun the reported source checks.

### Intent revision 5 — release candidate convergence (candidate is not acceptance)

- Revision 5 adds a reproducible release-candidate deliverable and a revision-bound
  acceptance matrix without changing the explicit live/device/off-host acceptance
  bar. The matrix above maps every current check ID to its user journey, components,
  owning lane, local evidence, live proof and external prerequisites. It is a
  planning/status snapshot, not a claim that active lanes have completed.
- **RC artifact definition:** versioned Tessera source/wheel candidate with
  deterministic inputs; separately pinned isolated observer source/patch and
  baseline identity; clean-install checks for package entry points, skills and docs;
  compatibility manifest; explicit secret/runtime-state exclusion checks; operator
  runbook; and a final matrix of exact artifact revisions and assigned gate receipts.
  Keep redistributable source/wheel separate from `.tessera` receipts and isolated
  worktrees. Exclude credentials, auth/config state, caches, runtime databases,
  private guard files and unrelated worktrees. RC construction and checks do not
  imply commit, publication, deployment, or production release acceptance.
- **Accepted local/source checkpoints (owner receipts; no checks rerun in this
  journal step):**

  | Lane / changed-file scope | Recorded receipt and revision | Boundary still unproven |
  |---|---|---|
  | Agent provenance: `src/tessera_control/agent_provenance_models.py`, `agent_provenance_harness_models.py`, `agent_provenance_harness.py`, `agent_provenance_store.py`, `agent_provenance_harness_projection.py`, `guard_phase_provenance.py`, `tests/test_guard_phase_provenance.py`, `docs/guard-phase-provenance.md` | 102 tests, 122.22s, 2 warnings, exit 0; source SHA `c70e2b91b5cbe31861d49b0f3eb19cec672190a05e7362adc69c258e5d1ebc13`. Temporary HTTP/SQLite append/replay/export/disclosure/rollback; absent optional legacy fields preserve serialization. | Synthetic guard/owner facts; contract is always non-admitting. No private guard or live feed. |
  | Guard join/factory: `src/tessera_mcp/guard_phase_evidence.py`, `src/tessera_mcp/guard_phase_join.py`, `tests/test_guard_phase_evidence.py`, `tests/test_guard_phase_join.py`, `docs/guard-phase-evidence.md`, `docs/guard-evidence-contract-decision.md`, `src/tessera_agent/research_factory.py`, `tests/test_rc_research_factory.py`, `docs/rc-research-factory.md` | Factory receipt: 11 tests, 5.48s, 3 existing warnings; SHA `97311053d3056ec11c79ffe27aa06a8da96ce5bc19a60dcba453c849303ff3dd`. Local SDK publisher/read-consume→durable SQLite factory; strict admission unchanged, synthetic credentials/authority, zero dispatch/network. Earlier 21-case channel receipt retained separately, not added. | No genuine account/owner guard, approved principal, external authority, live session, or admission. |
  | Isolated public runtime/SDK publisher | Source SHA `6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`; handoff reports 52 Bun +4 Python checks and two reproducible isolated builds. Native checks were repeated; do not count as 112 unique cases. The earlier wheel receipt is historical; corrected exact-wheel protocol evidence is recorded below. | Synthetic credential/principal/resources and protocol outputs; zero research dispatch. No actual account, provider inference or final transport proof. |
  | RC packaging and observer distribution: `pyproject.toml`, `MANIFEST.in`, `dev/build_release_candidate.py`, `dev/check_release_candidate.py`, `dev/observer_snapshot.py`, `tests/test_release_candidate.py`, `docs/release-candidate.md`, `docs/release-checklist.md`, `release/opencode-observer/{README.md,baseline.json,LICENSE,observer.patch,baseline.tar.gz}` | Packaging source SHA `cd7d7f14b55e268fe343dedb58c19f7fb9b07575a5fc192cb4d9d0d6dbd6c8b6`; 37 tests/Ruff passed. Five-file bundle staged; 27-original/36-target pins, license and patch reconstruction checked. Packaging boundary tests: 12 passes, synthetic packaging only. | Exact-wheel local install/protocol run now exists, but that source archive predates the builder lock fix; corrected source archive/final candidate still needs rebuilding. Root source/license/redistribution review remains pending. |
   | Exact-source gate runner: `scripts/run_release_candidate_gates.py`, `tests/test_release_candidate_gates.py`, `docs/release-candidate-gates.md`; protocol harness: `dev/check_protocol_compatibility.py`, `tests/test_rc_protocol_compatibility.py`, `docs/protocol-compatibility.md` | Runner SHA `5eaec814d65487ffb00e88d47fbc40144709ab440017955c5554d082fc43135e`; prior runner unit receipt 43 pass/0. Latest retained exact-wheel protocol record ID `LOCAL-PROTOCOL-V5-FINAL`: one test/eight stages/29.61s/native exit 0 for wheel `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99`; independent local A2A and actual MCP stdio, synthetic outputs. | This record binds only the retained pre-repair wheel. MCP native child exit remains unknown; owned peer `-15` is cleanup. Neither it nor focused regressions constitutes the current full source gate. |

  Historical runner checkpoints 31 and 39 are superseded by 43, not additive;
  prior baseline full-gate 4,268 is not current-candidate evidence. The table reports
  distinct lanes and does not sum overlapping test runs.
- **Candidate/installed-wheel status (receipts now available):** the initial
  `0.1.0rc1` source archive and wheel were byte-identical across two builds:
  source archive SHA `13a28991f2f628e46fc78381ae97d88400aa4b22b7044334db49b5691c3868cc`,
  wheel SHA `5553cecb3051122ee103da7bbc6394ccb6254ecb47057af5eb57803529ff49c5`.
  The source archive staged `pyproject` as 0.1.0rc1 while frozen `uv.lock` retained
  project version 0.1.0; offline lock validation failed. That source archive and its
  gate attempt are superseded, not accepted as current candidate evidence. Initial
  double-build local record ID `RC-V5-INITIAL-DOUBLE-BUILD`.
- The bounded staging fix changed only `dev/build_release_candidate.py` and
  `tests/test_release_candidate.py`; root `pyproject.toml` and `uv.lock` are
  unchanged. Its 17 targeted tests passed (2.95s); scoped Ruff lint/format/whitespace
  and corrected offline `uv lock --check` passed. An initial locked offline sync
  reached 58 package resolutions but failed because cached PyYAML 6.0.3 was absent.
  After authorized public-package acquisition, 50 pinned public wheels (15,894,784
  bytes) were individually size/SHA checked; public locked sync passed (8.248s),
  subsequent offline locked sync passed (0.250s), and offline lock check passed
  (0.199s). This is an explicit dependency artifact set, not a hermetic bundle or
  live provider test; the environment reports 51 installed distributions matching
  lock versions. Canonical repair local record IDs:
  `RC-V5-STAGED-LOCK-FIX` and `RC-V5-PUBLIC-LOCKED-INSTALL`.
- A later v5 package iteration (not the source snapshot underlying the 4,503-case
  gate) was source archive SHA `fbd6a29cc5346f50701c5df92855ec36376f9b1f7c3203df16222305233adc24`
  and wheel SHA `60ec375725e94f9e6a2d1cfbeb243af5944b296a155db38027b7d0b12e72ac99`.
  Both builds were byte-identical; locked dependency sync, exact noneditable wheel
  installation, five CLI helps, resource/origin, observer and secret-exclusion checks
  passed. Local record ID `RC-V5-FINAL-PACKAGE-HANDOFF`. This is local package evidence, not fresh backend dependency
  acquisition or a hermetic bundle. It is not the current candidate after subsequent
  protocol/provenance test-fixture and journal edits.
- On that exact wheel, installed protocol passed one test/eight stages/29.61s/native
  exit 0. Local record ID `LOCAL-PROTOCOL-V5-FINAL`
  (SHA `b908a13ce254f164ef0250f67a1f8f1a3c92707104e5d593bf8ba5f5aa1cb2b0`), exact
  wheel SHA above; 248 product files and 136 imports matched. Actual local MCP stdio,
  backend and independent A2A peers; critic output/model/source and human fixture
  identities/resources are synthetic. Peer `-15` is owned cleanup; MCP native exit is
  unknown. Installer archive SHA is null/unknown; binding is exact local wheel URL
  plus independent wheel SHA and installed bytes/RECORD/metadata/origins.
- The previous full-suite attempt (local record ID `RC-V5-SIX-FAILURE-DIAGNOSTIC`)
  was 4,444 passed/6 failed/26 skipped/3 warnings
  with source drift and is superseded, not current acceptance. Six node IDs were
  recovered from pytest cache, but the raw error bodies and skip reasons were already
  discarded and cannot be reconstructed; do not label the 26 skips expected or
  optional. Owner-directed exact-case checks resolved five of six; the remaining
   optional `guard_phase_binding` capture fixture was repaired without removing
   mandatory source/principal/authority integrity checks. Other repairs were narrowly
   fixture-scoped: `tests/test_protocol_installation_guard.py` now uses a valid
   relative third-party `.pth` entry rather than an absolute temp path containing the
   Tessera repo name; production origin guard stayed unchanged and its targeted 33
   cases pass. `tests/test_research_split_context.py` preserves required integrity
   checks while not fabricating absent optional `guard_phase_binding`; repaired file
   SHA `3375cfce4efbc1873f728f24fead149ccc1ddf71bfa34a1290681527ea5a7100`. Three
   packaging exact cases pass with native Python 3.11.6 and setuptools 81.0.0; no
   packaging implementation fix was needed for those cases.
- The complete pinned gate-driver environment is now ready; its local record ID is
  `RC-V5-COMPLETE-GATE-DRIVER`. Native
  Python 3.11.6, setuptools 81.0.0/bundled wheel 0.46.3, pytest 9.1.1, Ruff 0.16.6,
  MCP 1.26.0, A2A 1.2.1 HTTP server and locked dependencies were verified. In that
   environment all six original failed node IDs passed together: 6 passed, 0 failed,
   0 skipped, 3 warnings, exit 0. Exact node receipt:
   local record ID `RC-V5-SIX-CASE-RECOVERY`.
   This is focused regression recovery—not full Ruff,
  format or repository pytest gate. Earlier 26 skip reasons remain unknown. No test
  driver changes the installed Tessera product/runtime versions.
- The final artifact freeze was revoked after the protocol `.pth` and provenance
  optional-field fixture repairs. The retained `fbd6…`/`60ec…` build and protocol
  receipt remain valid only for those exact artifacts, not the repaired source.
  This documentation batch is intended to be the final source-journal update; root
  must now establish writer quiescence, capture the repaired source inventory and
  rebuild/reinstall/rebind protocol evidence before running the exact current-source
  gate. Earlier full-gate attempts remain superseded; no current full gate has passed.
- **External evidence/decision boundary:**

  | Evidence group / decision owner | What is measured now | External fact or decision still required |
  |---|---|---|
  | Local guard/quota tests / runtime owner | Source executor, SDK channel and durable factory code paths execute locally; fake owner/upstream/authority facts; zero dispatch/network. | Genuine owner guard/quota evidence, authenticated enrolled principal and final provider route are unmeasured; local fixtures cannot authorize admission. |
  | Application provider account / authorized provider owner | No application account call, inference, entitlement or billing inspection in this checkpoint. | Provider entitlement/billing and no-fallback are unknown. Any later live run needs actual approved account authority and its separate evidence; coding-agent quota is unrelated. |
  | Operator financial controls / authorized operator | No financial setting changed or independently read; observer contract preserves `not_supplied` and freshness unknown. | A separately sourced operator attestation with exact scope/freshness; never derive it from tests or receipt defaults. |
  | Public observer bundle / root reviewer | Five files and 27-original/36-target pins, license bytes and patch reconstruction are staged/checked. | Root content/license/redistribution review is not recorded as approved. Keep final candidate distribution gated on review; file hashes are integrity, not approval. |

  These are separate evidence classes, not fields to merge into a synthetic pass.
  Do not infer values from local tests or coding-agent quota, insert success defaults,
  or widen admission.
- **Final gate is deliberately still pending:** the prior exact-wheel protocol pass binds only to the retained `60ec…` artifact, not the repaired test/doc source or actual research admission. The full Ruff/format/repository pytest gate has not run on current source. `native-picker`, `offhost-execution`, `remote-reasoning` and `full_acceptance` remain `live/not_run`. Focused six-case recovery is not a substitute; root still must confirm all writers quiescent, capture/rebuild/install the repaired source and rebind protocol evidence before full gate.
- **Quota admission checkpoint:** the prior quota-based admission hold is resolved
  by normal fresh admission for the same preserved owners; no replacement worker,
  paid fallback, top-up or policy override. Current telemetry reports 94% remaining
  at 01:37 UTC 2026-10-07. This is coding-agent inference capacity only, not
  application provider entitlement, billing or guard evidence.
- **Exact-artifact convergence remains pending:** this is the planned last source-
  journal batch. Once every writer is stopped, root freezes the repaired exact source,
  captures inventory, deterministic archive/wheel builds, exact install, protocol
  rebind and full-source gate under the canonical receipt locations. The retained
   preceding artifact/protocol record IDs are `RC-V5-FINAL-PACKAGE-HANDOFF` and
   `LOCAL-PROTOCOL-V5-FINAL`;
  they must not be relabelled current. Complete gate-driver handoff and six focused
   case record IDs are `RC-V5-COMPLETE-GATE-DRIVER` and `RC-V5-SIX-CASE-RECOVERY`.
   Record final gate outcomes in receipts/task evidence,
  avoiding further source-doc churn solely to mirror those results.
  Do not treat the historical 4,268 pass (pre-v4), worker-local fixtures, or an RC
  package build as proof of
  real planner/critic reasoning, production admission, human experiment/review,
  physical-device, independent-host or laptop-off acceptance. Those live gates stay
  separately open.

### Intent revision 6 — explicit evidence-backed runtime admission, not live admission

- The previous contract semantics are corrected by reviewed intent-6 decision
  [0029](decisions/0029-evidence-backed-runtime-admission.md); the versioned
  [runtime-admission-v2 contract](runtime-admission-v2.md) and its old→new evidence
  table in [`guard-evidence-contract-decision.md`](guard-evidence-contract-decision.md)
  are authoritative. Strict legacy v1 remains the default. V2 is explicit human
  opt-in with a separate closed schema; no legacy boolean conversion, implicit
  version upgrade, complete/verified escape field, fake provider pin or fallback.
- Semantic resolution: v2 may classify provider entitlement/billing as unknown only
  under explicit approved policy; unknown required **local enforcement** still
  denies. It separately requires completed local OAuth-method/route/account-match,
  approved endpoint/model and no-key/no-custom-override/no-paid-fallback checks,
  fresh quota and installed runtime/SDK/config/skill/catalog pins. Financial settings
  come from a scoped authenticated operator assertion with issuer, owner/project/
  destination/policy scope, observed/asserted times, explicit maximum age, expiry and
  current revocation—not a research grant/capability or inferred test value.
  Preparation is ready-without-dispatch. Final HTTP/WS verification must follow
  mutations and occur pre-effect, with exact wire/operation binding and one-shot
  native capability; no post-dispatch join substitutes for the guard.
- Implementation is recorded in `src/tessera_mcp/runtime_admission.py` plus the
  harness binding/observer and agent service/factory/consumer integration; exact six
  source, two test and three documentation path hashes are in private handoff
  local record ID `RUNTIME-ADMISSION-V6-FINAL-CONTRACT`.
  Final contract artifact revision SHA is
  `acf8d8c6b4b42851ba53af29d4bc708e03e2fd7324c5aa7c2e049bf43b9ed32c` (not an RC
  digest); its sorted source-inventory SHA is `966179578d579a9da6c11c9d6f3ed7b74b78e67fd471aebe36f90633d085f8cf`.
  Isolated native source revision `59d771e9ba6dc1db15af4915932ccc99f84dc7dab938aaa54f3fb4718d438f69` and Python verifier module SHA
  `cb81a5878fad77955a59b56673e52400b17af6b2c9e998e414d368606265de94` are distinct
  namespaces; neither claims a built/installed release.
- Distinct local receipts, not additive: verifier 106 passed; affected verifier/
  factory/service integration 123 passed; native terminal gate 58 plus 10 preparation
  cases passed; 106 verifier cases overlap the 123 affected integration selection
  and are not additive. Interop has 14 repaired HTTP plus 8 unaffected WS cases with
  native exits verified (there was no single 22-case pass invocation). The final
  isolated terminal inventory records 58 native +10 preparation/auxiliary passes and
  33 original/45 target patch reconstruction. Exactly 105 existing private receipt
  files were independently hashed; full commands, stdout/stderr, exits, source
  inventories and failures remain retained under the handoff. All authority, operator,
  enrollment, quota and upstream resource facts in local execution were synthetic.
  Native fake effects exercised actual HTTP/WS terminal code; Python SQLite restart
  tests establish only that replay ledger's persistence, not native durable atomic
  grant/budget claims or production authority.
- Concrete remaining integration requirements: genuine approved owner/principal/
  session/publisher/issuer enrollment and current authority; authenticated operator
  financial issuance, reference/current-revocation and profile-age resolver; private
  OAuth guard and same-check fresh native quota adapters; durable atomic native grant/
  budget reservation, uncertain disposition and reconciliation; approved installed
  Python/native bridge; independent installed SDK/build/effective-config and full
  loaded-skill/catalog/tool-map readbacks. No live inference/provider request was
  made or authorized in this step. These are missing trusted adapters/bindings and
  deployment facts; the reviewed v2 semantics do not need another generic approval.
- The original v5 full-gate candidate remains immutable history: source
  `3bd2d1a850951fa23d64a23ff8b9cbd1fb25deed3460a82d550c0884ee799145`,
  wheel `fc537c837d3543656add8e696ff468615449b4f88479246bea2a60da6a649eea`; its
  4,503-case/Ruff receipts apply only to that exact source. The v6 files and isolated
  patch invalidate current-candidate readiness. Candidate-impact receipt
  local record ID `RUNTIME-ADMISSION-V6-CANDIDATE-IMPACT`
  records the affected package/install/protocol/full-gate checks and required new
   freeze. No v6 candidate build or current full-source gate is claimed. This was the
   planned journal batch at intent revision 6; the subsequent v7 source work and
   canonical reconciliation below supersede that planning note, not its exact v5/v6
   artifact history.

### Intent revision 7 — local runtime-binding source convergence (not production admission)

- The backend/API, native owner/client, verifier and source-closure lanes are now
  quiescent. The concrete integration executes actual Tessera Store/API/SQLite,
  authenticated temporary HTTPS and Unix IPC, the original private guard factory
  (read-only, with test-owned resources), the native quota command/predicate, Python
  verifier before fresh claim, actual SDK-lowered branded HTTP/WS effects, canonical
  writer fence through child exit, native private lock, and conservative settlement.
  This closes the previously incomplete local component binding; it is not installed
  production admission or the planner/critic research journey.
- Final paired receipt:
  local record ID `RUNTIME-BINDING-V7-PAIRED-NATIVE`.
  Backend, returned-root fence repair, and schema-55 legacy fixture record IDs:
  `RUNTIME-BINDINGS-V7-BACKEND`, `RUNTIME-BINDINGS-V7-ROOT-FENCE`, and
  `RUNTIME-BINDINGS-V7-LEGACY55`.
  Isolated native implementation/source details:
  local record IDs `OBSERVER-V4-BINDINGS-V7`, `NATIVE-SOURCE-CLOSURE-V7`, and
  `OBSERVER-V4-SOURCE-HANDOFF`.
- Counts are separate selections/composite evidence, not one additive suite: backend
  32, migration fixtures 18, API/authority 31; native binding has 82 scoped
  native/owner/process/cold-export tests plus separate HTTP/journal and preparation
  selections. Paired native evidence covers 25 distinct cases in retained composite
  receipts (original 24 plus one claimed-next-root case), not one 25-case passing
  invocation. The latest affected reruns were 4 passed/21 deselected (exit 0) and
  2 passed/24 deselected (exit 0); the original failures remain retained. The cold
  source closure is a separate 79-input inventory (62 unchanged + 17 public licensed
  originals), namespace SHA
  `ea7e77189b760f4d63bd3368dffe014b00d54daa12555b33d412f79f59817c2a`; actual cold
  Bun SDK/HTTP/WS and Python source gates exited 0. This is source linkage, not a
  full package build/install. Do not add counts across overlapping or distinct runs.
- Native paired behavior observed: same enrolled test fixture's branded WS handshake
  and frame each crossed the real target boundary once; readback and exhausted third
  call caused no extra effect. Claimed/uncertain roots remain fenced; a later
  independent request is allowed only after immutable native-returned settlement and
  fresh server-issued refs. The last-unit reservation is consumed by its own claim.
  This is not atomic DB-plus-network delivery, provider exactly-once, or billing proof.
- Upstream OAuth/account/quota, private-owner resource context, temporary identities,
  installation-linkage callback and fetch/WS primitive resources were synthetic.
  No real provider dispatch occurred. Still required: independently approved genuine
  principal/session/publisher/issuer registration; owner-held private guard/quota and
  financial/revocation/profile-age handles; approved installed SDK/build/runtime,
  effective config, skill/catalog/tool-map and process linkage; and the actual
  off-host/device/live planner/critic/review journey. Production remains fail-closed
  until those resources are securely registered. No credential discovery/copy,
  active service change, deployment or commit occurred.
- V7 is source convergence only. No release candidate was built; no v7 full-source
  gate, clean install, final wheel protocol rebind, provider run, or independent-host
  acceptance is claimed. The v5 4,503-case/Ruff gate and v5 wheel evidence remain
  historical and cannot be relabelled. The companion read-only OSS/tooling assessment
  is [`docs/oss-tooling-review.md`](oss-tooling-review.md); recommendations there do
  not install or authorize optional tools.

## Remaining items / next actions

- The prior run-5 late/quarantined/cancelled outcome and run-8 quota-403/unknown
  accounting are still preserved; schema-52 success does not reconcile or rewrite
  them. Revision-5 adds bounded local provenance, SDK/publisher/factory, packaging,
  observer-distribution and source-protocol evidence detailed above. The 4,268-case
  gate remains passed only for its frozen pre-v4 snapshot; no fresh full gate exists.
  Final source/wheel/clean-install checks, installed protocol, genuine principal and
  guard/admission, live reasoning, experiment/review, session restart/cancel and
  production notebook join remain open; root acceptance is not closed.
- **Next executable acceptance:** confirm every writer has stopped and explicitly
  freeze the exact source tree; build two identical final candidates, run the
  revision-bound full local source gate and final clean-install/exclusion/compatibility
  gates, then record exact hashes/receipts at the reserved paths. The retained prior
  installed protocol receipt is not bound to repaired source; re-run/rebind it to the
  final exact wheel. Only after legitimate trusted owner
  guard/principal and provider/operator facts exist can the real planner/critic,
  human experiment/review, active session lifecycle, garage/off-host/device/laptop-off
  journeys be evaluated. Preserve the stricter verifier and prior deployment refusal.
- **Research-profile prerequisite still pending:** admit a dedicated included-OAuth
  reasoning profile only under the exact guardrails above. Intent revision 2 now
  explicitly authorizes the garage-server Docker host `${GARAGE_USER}@${GARAGE_HOST}`
  (`linux/amd64`) for independent-host testing; local Docker Desktop Kubernetes
  remains authorized for supporting checks. This changes target authorization,
  not evidence: no garage host inspection/deployment or laptop-off workflow is
  recorded in this checkpoint. Before any scoped deployment, use the garage-server
  runbook and existing SSH/runtime references; inspect BatchMode SSH availability,
  explicit Docker context, host capacity, current workloads and ports. Preserve the
  LAN scanner on port 80, unrelated services/data and default Docker context.
  Do not install arbitrary Kubernetes, read/copy OAuth values or renew expired
  credentials. Verify a secure included-OAuth research-profile handoff/admission;
  if absent, record the exact missing reference rather than substituting a token
  or paid inference. Client disconnect is not physical laptop-off proof.

### Intent revision 2 — authorized remote target, not yet exercised

- User authorized `${GARAGE_USER}@${GARAGE_HOST}` as the garage-server Docker target
  (`linux/amd64`, approximately 7 GiB) for independent-host testing, and reaffirmed
  local Docker Desktop Kubernetes for supporting checks. This supersedes the older
  “no named off-host target” assumption; it does **not** report a host connection,
  inspection or deployment. No service/Job effect is claimed here.
- Preserve the schema-52 local success receipt and all historical receipts. Local
  authority/robustness/notebook evidence and the 16-stage on-time installed result
  remain valid at their recorded boundary. Local full-MCP and two-peer A2A evidence
  remain bounded local evidence only. Actual garage-host install/runtime/MCP/A2A,
  real critic reasoning and whole remote journey are outstanding; the reference
  A2A critic has null model and did not generate reasoning. Independent client
  disconnect is not physical laptop-off acceptance.
- The current-source gate remains uncollected after the same journey child's
  repeated `misalignment_policy_violation` admission denial. The newly authorized
  host does not override that denial: do not retry collection until legitimate
  same-child admission is resolved, and do not use an alternate worker/model or
  bypass. Quota telemetry remains healthy (23% at 00:28/00:29 UTC; latest report
  22%), not exhausted. The gate status is neither a test failure nor a pass.
- Next authorized host sequence belongs to the root/operator: use the garage-server
  skill and existing SSH/runtime references; first verify BatchMode access,
  explicit Docker context, capacity, workloads and port ownership. Then choose a
  narrowly scoped deployment while preserving scanner port 80, unrelated
  containers/data and the default Docker context. Do not install arbitrary
  Kubernetes or copy OAuth credentials. No secure included-OAuth profile handoff
  or admission is evidenced yet; inspect safe credential references, report an
  exact missing handoff if needed, and never fall back to paid inference.

### Intent revision 2 follow-up — inventory and portable-profile work blocked before effects

- The same deployment child `ses_ef8b39b5effeB4gZR6s7tenGiF` was rebound from the
  denied gate-collection objective to **read-only garage inventory**. The garage
  skill was loaded and SSH BatchMode, explicit context, workloads, capacity,
  scanner-port-80 preservation and safe-auth-reference metadata checks were
  prepared; coding-agent admission was denied with `misalignment_policy_violation`
  / `Potentially unintended activity` **before any SSH or Docker inventory call**.
  The child is paused with no active descendants. Host ports, capacity, workloads,
  SSH access and runtime-auth metadata were not inspected. Do not describe access or
  credential references as missing/unavailable; their status is unverified. No
  deployment or remote effect occurred.
- Separately, the same runtime leaf `ses_ef8b67654ffezx5qE5yX0Q0OdK` was rebound
  to implement the portable ordinary Docker runtime profile, owned at
  `deploy/remote-agent-platform/docker/**` and
  `tests/test_remote_agent_docker_profile.py`. Its actual `functions.patch` edit
  returned `permission.rejected: Permission denied: edit`; no files were created or
  changed, and no tests, host/Docker commands or inference ran. Existing runtime,
  client and service files remain unchanged by this attempt. The portable profile
  remains an implementation item: current root Compose is development-only and
  the root Dockerfile is lab-focused; neither includes the MCP+A2A extra profile
  needed for ordinary installed operation.
- No alternate host, worker or model was selected; no authority, model or spend
  bypass occurred. The existing schema-52 local installed 16-stage run, output
  proof (36 bytes, value 385, `late=false`), eight joined peer acknowledgements,
  and historical receipts remain valid at their recorded boundaries. A2A's
  protocol/reference result (null model) is not generated critic reasoning. Full
  garage runtime/MCP/A2A and integrated journey, real reasoning, laptop-off proof
  and current-source gate remain blocked/unverified. The gate remains uncollected;
  the separate inventory denial did not retry it. Coding quota at 00:46 UTC was
  healthy at 22%, not exhausted; no supported spend occurred.
- Root next actions: restore legitimate admission for the **same deployment child**
  to perform the authorized read-only inventory, without substituting a host or
  worker; restore permitted edit capability for the **same runtime leaf** to
  implement and test the portable profile. Afterward inspect host/context/resources/
  ports before any scoped deployment, preserving scanner port 80 and unrelated
  services/data. Separately resolve same-child admission for the already-existing
  current-source gate before retrieving it. Verify safe SSH/OAuth reference
  metadata only when permitted; denied calls establish nothing about credential
  availability.

### Intent revision 3 — source checkpoints do not complete the live journey

- Local authority and robustness evidence is unchanged and remains passed at its
  original local boundary. The schema-52 installed receipt also remains a valid
  **historical slice**: 16 configured local stages, on-time (`late=false`) output
  value 385/36 bytes with supported success, and eight reference-peer ACK records.
  It is not strengthened into revision-3 generated reasoning, hybrid, off-host, or
  complete integrated acceptance; old cancelled/quota-unknown receipts remain
  preserved.
- **MCP binding source checkpoint (partial):** files reported in the current
  worktree are `src/tessera_mcp/harness_binding.py`,
  `skills/tessera-governed-research/SKILL.md`,
  `tests/test_mcp_harness_binding.py`, and `docs/mcp-full-workflow.md`.
  `generate_binding`, `generate_from_mapper`, `catalog_sha256`, and
  `verify_binding` generate/verify source configuration and admission observations;
  **45 binding tests** and **46 binding-related regressions** are the latest
  reported source groups; earlier 44 binding tests are superseded, not additive.
  A temporary-backend SDK fixture exercised 13 outputs and 18 named-catalog
  parity/negative scripts were checked. Three agent tools expose registration and
  metadata reads, not human MCP approval. Scoped lint/format/diff passed. This is
  not live permitted catalog/config/account/skill discovery or account proof; the
  running consumer still does not install/accept these bindings.
- **Public interaction provenance source checkpoint (partial):** reported files
  include `src/tessera_control/agent_provenance.py`,
  `agent_provenance_harness.py`, `agent_provenance_harness_models.py`,
  `agent_provenance_harness_projection.py`, and
  `tests/test_public_harness_provenance.py`. Historical **85 focused tests** and
  latest source-54 reports of **137 affected**, **25 compatibility**, and **2
  recipient follow-up** cases are separate overlapping groups, not an additive
  total. They cover strict public metadata, human-context and skill/config pins,
  replay, registered-output projection and explicit gaps. Schema-52 receipt/evidence
  is unchanged. The separate schema-53 generated-public-output registry below now
  supplies source-level registration; actual V2 capture/runtime integration and
  human consumer/review remain outstanding. Never fabricate a Job/output receipt.
- Public-capture source paths are `src/tessera_control/public_capture.py`,
  `src/tessera_control/public_capture_outbox.py`,
  `tests/test_public_capture_outbox.py`, and `docs/public-capture-interface.md`.
  Reported **52 isolated tests passed in 5.04s**, with scoped Ruff,
  format/whitespace checks and synthetic HTTP. Normalized metadata/trust interfaces,
  private SQLite schema 1, stable replay/crash redelivery, strict ACK handling and
  independent source/delivery watermarks are tested; the existing agent client is
  unchanged. This remains a source fixture: actual V2 public-schema extraction,
  `V2Harness.status` extraction and `ReasoningSupervisor.tick` capture/drain callsite
  are unsupported/unverified. Runtime capture is not claimed.
- **Schema-53 generated-public-output registry source (partial):**
  `docs/generated-public-output-interface.md` describes the additive family and
  registration contract. The reported backend slice includes actual human
  admission, agent registration, separate human disclosure/revocation, immutable
  bytes and native family records without a Job receipt. Its **20 final targeted
  tests** and **31 migration/collector group** are distinct/overlapping evidence;
  do not add them into a larger pass. Source-54 registered-output/public-capture
  integration also has 137 affected, 25 compatibility and 2 recipient follow-up
  cases as separate overlapping groups. Backend snapshot digest
  `e752635898d04fca4987ee72b905ce4d55719032f289e29c7e4e1eb07407e5b1` predates the
  latest worker and is not whole-source proof. Source-54 reports a typed local
  registry→capture/provenance/A2A notebook join using synthetic bytes; source-level
  runtime consumer/reader/hook is now implemented, but live V2 public output
  generation, trusted observation, service factory and human review remain missing.
- **Generated A2A source checkpoint (partial):** reported files are
  `src/tessera_control/a2a_peer.py`, `a2a_store.py`,
  `tests/test_a2a_generated_store.py`, `tests/test_a2a_generated_peer.py`, and
  `docs/a2a-generated-output.md`. Historical 93-test results and 97+1 earlier
  checkpoints are retained; the latest report is **67 focused tests**, not summed
  with prior groups. Source exercises official-SDK routes, typed output/capture
  notebook join and five receiving transport records using synthetic model bytes.
  Older TestClient/SQLite/synthetic authority results remain bounded component
  evidence. Source enables registered generated-artifact references and a
  separately authenticated receiving-planner ACK; it does not prove independent
  live critique or deployment. Historical null-model reference peers remain
  protocol evidence, not generated critique. The latest 67-test report supersedes
  no history; prior 97+1 and 93-test checkpoints are not added.
- **Ordinary Docker worker source-54 checkpoint (bounded adapter, not research
  runtime):** modules are `ordinary_docker_service.py`, `ordinary_docker_messages.py`,
  `ordinary_docker_backend.py`, `ordinary_docker_worker.py`,
  `ordinary_docker_guard.py`, `ordinary_docker_transport.py`,
  `ordinary_docker_http.py`, `ordinary_docker_contract.py`, and
  `ordinary_docker_execution.py` under `src/tessera_worker/`. Tests are
  `test_ordinary_docker_guard.py`, `test_ordinary_docker_http.py`,
  `test_ordinary_docker_control.py`, `test_ordinary_docker_transport.py`,
  `test_ordinary_docker_service.py`, and `test_ordinary_docker_execution.py` under
  `tests/`. Operator docs are `docs/ordinary-docker-control.md`,
  `ordinary-docker-http-binding.md`, `ordinary-docker-execution.md`, and
  `ordinary-docker-worker-service.md`.
  The fixed-reference CLI `python -m tessera_worker.ordinary_docker_worker` now
  binds enrollment, native poll/claim/current-authority/report/readback and exact
  execute versus stop-only receipt semantics. The final HTTP/service/CLI group
  reports **112 tests in 67.85s** with two dependency warnings using temporary
  HTTP/SQLite and synthetic Docker effects. Earlier 89-adapter, 31-service,
  47-initial and 37-readback groups overlap; do not add them or treat them as live
  Docker. Explicit worker credentials and verified TLS are used by the source
  adapter; no host shell, owner proxy, ambient daemon context or agent socket.
  The source-54 update also refreshes 14 current-contract cases/assertions across
  seven files while preserving the historical schema-53 fixture.
  `Dockerfile.guard` was not built or enrolled; no live container/host effect.
  This adapter is not the still-missing portable guarded research profile or
  generated planner/critic runtime.
- **Runtime source and attestation boundary (same leaves):** source now implements
  the default research factory, consumer/store, generated-output reader, peer
  transport, supervisor capture hook and `V2AttestedHarnessObserver`. The default
  factory uses actual configured HTTP/MCP-stdio/A2A/context/capture/registry/
  human-record/input/experiment ports and fixed `GuardTrust` references/pins; it
  has neither a fixture-configured production port nor an unconditional placeholder
  exit 78. Missing required proofs fail normal admission. The scoped 171-case
  targeted aggregate reported passed, three warnings, no skips, exit 0, and Ruff
  passed on source digest
  `a3eda2bd89d181b45f8d2140457d4b4c9a8da6e1a834b83316b8cbb0da333e01`. This
  supersedes the 133-affected targeted gate after observer-adapter changes; it is
  **not** a fresh run of the historical 3,454-pass/1-skip full Python gate. No
  linked gate-receipt path was supplied with the current source-digest evidence.
  - The 171 scope used temporary HTTP/SQLite, MCP stdio, and two independent A2A
    SDK server instances. Model output, admission facts, guard measurements and
    peer identity/control in those fixtures are synthetic; this is local executable
    integration, not actual provider reasoning or trust-origin validation.
  - Separate-context source digest
    `97cf201898027afdbacd2b43575a66d6652f5bc5b6997e1aa064393bc7bf638a` has a
    separate 27-focused-test/exit-0 result. A 130-case print-summary run had a
    clean printed summary but unknown process exit; it is not added to the 27 or
    171 counts. Earlier 20-case observations span separate runs, including one
    constructor failure subsequently repaired; do not call them a fresh aggregate.
  - The shipped observer plugin/Python RPC protocol and `V2AttestedHarnessObserver`
    source verify issuer trust, fresh nonce/30-second age, revocation, closed
    fields, and pinned config/catalog/skill/account/hook observations. That proves
    only what an issuer attested, not that the issuer's measurements are truthful.
    Separate observer-case receipts report 26 synthetic cases plus 3 age-boundary
    cases; these are component coverage, not another aggregate and are not summed
    into the 171-case result.
    A trustworthy guard producer and dedicated origin/attestation admission have
    **not** been implemented/shipped/supplied. The contract path
    `/tessera/runtime-guard/v1/observe` is Tessera's specified integration contract,
    not an existing OpenCode API. Source configuration/manifest/account booleans
    cannot substitute for measurement.
  - Actual V2 safe SDK does not currently provide effective/final policy, complete
    harness-discovered catalog, selected raw skill/load body, supervised identity/
    build/schema SDK, selected included-OAuth entitlement/billing, or final request
    hook-chain/post-hook drift measurements. Issuer signatures authenticate origin,
    not those facts' truth. A producer needs a trusted independently measured
    handoff or an authorized safe OpenCode observability extension; decide that
    boundary before claiming full source closure. Do not weaken global policy,
    copy OAuth/provider credentials, inspect keychain, or use API-key fallback.
  - The ordinary autonomous consumer/factory source is now wired, but real generated
    planner/critic execution and truthful observer evidence are unverified. Human
    review/rejection still needs live acceptance. Provenance and MCP owner source
    results have been returned as source-level work; their fixtures remain synthetic
    at model/admission/guard-observation boundaries and do not complete live MCP or
    notebook acceptance.
  No source suite, full gate, host/auth operation, image build/deploy, environment
  file read, service start/restart or inference occurred in this documentation
  checkpoint.
  - Packaging source exists under `deploy/remote-agent-platform/docker/`:
    Dockerfile, Compose configuration, staging allowlist/script and operator
    runbook. Research startup uses the real default factory and fixed-reference
    observer; required unproven/missing guard evidence fails ordinary admission
    before effects. Four static/mapper cases and scoped Ruff were reported passing;
    these are not an image build or live profile check. No Docker build, deploy,
    auth/env read, or host operation occurred. The guard producer remains absent, so
    the packaged profile is not live-usable.
  - Ordinary non-env-reference source edits and packaging work on this same leaf
    succeeded through normal admission. The earlier denied edit was specifically
    a batch containing `deploy/remote-agent-platform/docker/.env.references.example`;
    that reference file remains untouched. No rename/substitute, config weakening,
    or retry was made. This corrects any prior overbroad implication of a global
    edit block; the configured `*.env.*` deny remains only a plausible explanation
    for that specific rejection, not a proven effective rule.
  - No source suites, gate commands, host/auth operations, permission changes,
    service starts/restarts or inference occurred in the latest diagnostic.
  - The original runtime `functions.patch` attempted a batch containing
    `deploy/remote-agent-platform/docker/.env.references.example` and was rejected
    with `Permission denied: edit`. Separately inspected V2 config
    `config/v2/opencode.json` lines 81–84 contains an `edit` deny for `*.env.*`,
    which matches that resource path. This is a plausible configured-rule match,
    **not proof** it was the effective rule in the original rejection. The old
    request ID, full resolved-resource list, feedback, actual agent identity and
    matched rule were not recovered. The configured rule is not proof of a global
    edit ban, a missing capability, or user rejection. The batch is atomic; do not
    retry the denied reference-file edit. Other source paths were not established
    as denied. The same runtime leaf may assess a genuinely distinct owned source
    effect only through ordinary admission; that is no authorization guarantee.
    The existing connected-client operator can inspect the original patch event
    and resolved resources/rule. Only if that confirms the issue, the owning
    operator decides whether to make a narrow public-reference-file exception or
    request a classification correction; do not change policy here. Use allow-once
    only for an actual pending request.
  - The same deployment leaf's read-only inventory attempt again stopped before
    SSH/Docker with `misalignment_policy_violation` / `Potentially unintended
    activity`; it is paused with no active descendants. Inspection of existing V2
    source reports `src/v2/tool/subagent.ts` forwards `result.info.error`, while
    `ai/protocols/open-responses.ts` formats provider error code/message; the SDK
    response enum includes this refusal code. This is consistent with a
    provider-layer refusal forwarded by the wrapper, **not proof of an edit
    permission prompt**. The historical raw provider response/request/run metadata
    needed to identify the issuer was unavailable. There is no documented
    unlock/allow-once, provider fallback or restart remedy; user continuation is
    not an override. Continue to treat host inventory as blocked.
  - Supported operator troubleshooting remains within the existing V2 client:
    inspect service/version and redacted run/role/error-code/request-ID metadata.
    On an already configured/authorized connection, official V2 OpenAPI lists
    read-only `GET /api/permission/request` (pending) and
    `GET /api/permission/saved`. Session-message listing is private operator
    inspection only; do not dump/share prompts or raw bodies. No credential copying,
    new service/API, automatic start, or support publication is authorized here.
    When request/run evidence is needed, use the existing client support path for a
    minimal reproduction and redacted logs; do not infer the refusal source without
    the original provider evidence.
  - No new tools expose a safety appeal/admission override. The correct next step
    is legitimate provider/session admission resolution by the owning operator;
    until then, no retry, replacement worker/model, policy bypass or host command.
  The guarded Docker profile and configured consumer are source-wired, but the
  trusted guard producer/evidence is absent; signed observer payloads authenticate
  the source, not the truth of its measurements. This is an actual external
  admission/measurement gap, not a missing observer-verifier or unconditional
  startup-placeholder issue. Source54 Docker worker transport alone is not live
  research-profile acceptance.
- The same backend leaf `ses_ef8baafc5ffeUV9vWUXTbMKrD1` reports the schema-53
  generated-public-output registry implementation and scoped test groups described
  above. Source-54 registry, capture, consumer, A2A and provenance are now connected
  in the bounded default-factory source path; the targeted receipts do not prove a
  full journey, truthful guard measurements, or installed-schema54 behavior.
- The bounded Docker adapter work from the same worker
  `ses_ef8b63612ffe42mteiN1Ar34K6` now has a source/test checkpoint (detailed
  below). Its completion is limited to the typed worker HTTP/service/CLI adapter,
  not live host deployment. The portable profile source is now present and wired to
  the factory, but remains unusable for authentic research until a trustworthy guard
  producer and dedicated origin admission exist; no image was built/enrolled.
- **Host and gate boundaries:** garage server remains explicitly authorized, but
  its read-only inventory child was denied admission before any SSH/Docker call.
  Host workload/capacity/ports and SSH/runtime-auth reference metadata remain
  uninspected—not unavailable. Do not retry the deployment refusal unchanged or
  use a replacement child/model. The current full-source gate is now passed at the
  frozen snapshot above; it does not clear the separate garage/deployment refusal.
  Included-OAuth coding quota was reported healthy at 22% at 00:46 UTC and most
  recently **5% at 00:43 UTC on 2026-10-06**; no exhaustion or credit risk is
  reported. Included-OAuth only; no paid fallback. No credential
  values were read/copied.
 - **Acceptance sequence still open:** the source consumer/factory and separate
  curated-input/human-policy stores are implemented and tested with synthetic
  facts. A partial authenticated RPC producer emits the available supported V2
  observations as `complete:false`; it does not grant admission. The unresolved
  boundary is a truthful complete trusted producer and dedicated origin/attestation
  admission. Root must decide whether to supply a trusted independently measured
  handoff or authorize a bounded external-runtime measurement extension; do not
  infer truth from config, manifests, account flags, or a valid signature alone.
  Do not weaken global policy, copy OAuth or
  provider credentials, inspect keychain, or use API-key fallback. After that
  admission decision, execute real generated planner/critic reasoning, human
  review/rejection, experiment/export and same-run restart/cancel. The packaging
  profile is wired but not built/enrolled; do not claim its source tests as live use.
  Do not retry the rejected runtime `.env.references.example` batch.
  Reconcile the returned MCP/provenance owner evidence without overstating its
  synthetic measurements. The owning operator should inspect the original patch
  event only if resolving that specific denial; any narrow exception/classification
  action must follow the actual matched rule.
  Any distinct source edit still requires normal admission. Resume garage inventory
  only after legitimate same-child provider/session admission
  resolution, not by bypassing the refusal. Then verify a secure
  included-OAuth handoff, stage garage control/reasoning with Mac Kubernetes as
  explicitly hybrid intermediate evidence, and complete equivalent independent
  off-host execution through the approved target or bounded Docker adapter. Verify
  active-run restart and cancellation of an actual running Job. Physical laptop-off
  remains a separate check. Keep the current eight-check status rows above; local
  rows stay locally verified; MCP/provenance source checkpoints have returned, but
  their live trust measurements remain synthetic. Remote-runtime/integrated-journey/
  journal-install remain blocked at live/install acceptance.
- Targeted `git status --short` reports several listed source/docs/test files as
  untracked in this already-dirty worktree. That shows their present worktree state,
  not when or by whom every file was created; preserve the state and do not
  attribute the whole untracked set to this checkpoint. This documentation update
  changed no source files.

- [ ] Record lane-to-owner assignments, interfaces, changed paths and dated
  milestones as work progresses; do not claim unobserved evidence.
- [x] Complete the frozen 3454-pass source baseline for its exact unchanged
  revision; all earlier failed/timed-out attempts remain historical.
- [x] Collect the frozen selected-source Ruff/four-shard gate: 4,268 passed, zero
  failures/errors/skips, Ruff 0.16.6 check+format exit 0. Receipt/HANDBACK:
  local record ID `SOURCE54-CONVERGENCE-GATE` (snapshot
  `b3677c4539c777bb90b6f64a939daf9a6ad6fdcaab22afd45175b5f3805b0c38`). Preserve
  the earlier 4,223+6 failure receipt and older 3,454+1 baseline separately.
  Later journal/backlog-only changes were not retested.
- [x] Implement local runtime/A2A redacted outbox delivery through the shared
  client and exercise collector append/replay/acknowledgement/denial semantics
  with local SQLite. Ordinary TCP service transport, runtime reasoning and full
  authorized end-to-end export acceptance remain open.
- [x] Run the bounded local real SDK/stdio/control MCP scenario with the same
  worker: 29 calls passed on current schema-51 control, including dual-cursor
  notebook/export, replay and denial/revocation/expiry. Earlier 503 remains
  historical.
- [x] Install the schema-52 platform wheel/image, pass its reported `pip check`,
  migrate preserved state and run the bounded installed journey. The fresh normal
  Job/output and peer events are joined; root integrated acceptance remains open.
- [x] Run the separate normal on-time worker path (renewal already exists), and
  collect the available sanitized run-5 timeline. Preserve run-5 late/quarantined/
  cancelled output as historical; exact cancellation reason remains unknown.
- [ ] Build/install the package and verify its registered `tessera-reasoning`
  command; secure the dedicated approved runtime profile and named authorized
  host before a live reasoning run.
- [x] Reconcile the original never-claimed, expired-worker dispatch through the
  original human issuer's stop-only maintenance authorization and actual scoped
  provider proof. Original identity/workspace/history remains preserved; no
  credential renewal or termination inference.
- [ ] Reconcile `docs/a2a-peer.md` packaging status with `pyproject.toml` and
  `uv.lock`; verify the dedicated `tessera-a2a-peer` console entrypoint from a
  clean installed `[a2a]` artifact (installed peer processes passed, but the
  dedicated entrypoint invocation is not separately established).
- [x] Implement A2A's redacted reasoning bridge/outbox through exact
  approved task-profile/digest mapping and test local scoped append/replay.
  Actual critic reasoning and authorized end-to-end export remain open.
- [x] Exercise runtime/A2A redacted outboxes over the installed service boundary;
  two peers acknowledged records and the schema-52 snapshot joined peer/output
  provenance. Revoked proof remains pending at its frozen epoch. Historical and
  off-platform completeness, real reasoning and root acceptance remain open.
- [ ] Execute the ordinary service templates on an authorized Linux systemd host
  and verify `LoadCredential` startup, fresh wheel/module entrypoints, graceful
  stop/drain and resume without creating replacement work.
- [x] Complete the sequential source gate for the previously timed-out shards
  on the frozen 8973... baseline; retain earlier attempt timeouts as history.
- [ ] Obtain current complete-source gate results after same-child admission
  recovery; the frozen baseline does not validate later changes.
- [x] Restore the owned local control endpoint and exercise restart generation in
  run 5; preserve the expired original worker and its journal. Any next worker
  identity must be a separately human-issued fresh run, never a replacement
  attestation for the stopped original.
- [x] Repair withdrawn-context discovery, owner-fenced Pod pagination and atomic
  connection-bound artifact save; focused local evidence is recorded above.
  Live Kubernetes and native picker acceptance remain separate and open.
- [ ] Run the connected native UI sequence (Open → released preview → Save →
  verify bytes/hash → reconnect) under documented Node >=22.12; do not count
  fallback file selection as native picker proof.
- [ ] Run assigned targeted checks and record exact commands/results, including
  negative authority cases, restart/reconnect and regressions.
- [ ] Exercise actual installed MCP/runtime/A2A components where available;
  clearly distinguish local Docker from a separate authorized host.
- [ ] Run integrated planner-through-MCP / critic-through-A2A journey with human
  bounded approval, denied expansion, restart, expiry/revocation and joined
  notebook export; live prerequisites must be explicit.
- [ ] At convergence, reconcile design/docs/backlog, report unrun gates and
  concrete remaining prerequisites. Do not close the root task from this lane.

## Intent 10 — documentation/privacy and runtime-composition checkpoint (2026-10-08)

- Public documentation now summarizes the exact measured `0.1.0rc2` candidate in
  [release evidence](release-evidence-intent9.md): source archive SHA-256
  `c19508014775fa23b5bf814cec561b82907b4204f035671ad41de18ed6cc2df0`, wheel
  `7237772c8b7fd1024fb29b40ea9555e4a2c1ed9c41b6fd75b61aefc69aaf876b`, frozen root
  namespace `95f121751817bee8a1eb7515fef2ffba1b0381af6e84dcbf25c10fdacc0dfc82`.
  Its exact source gate was 4,925 passed, 47 opt-in skips, 67 warnings, no failures
  or errors; installed protocol was eight stages with synthetic outputs; the local
  native slice completed three Jobs with 22 accepted renewals. These are bounded
  candidate/local facts, not production identity, generated reasoning, science,
  complete UI, training-process-crash restart, independent-host, laptop-off, or
  device acceptance. The cancellation settlement remains held/unresolved.
- The 723 artifact predates the intent-10 CLI/factory source and public documentation
  changes. It is retained as historical evidence, not a package for the current tree.
  No new build/install/native run was performed here; the root must freeze a complete
  post-writer inventory and rerun applicable candidate-bound gates after review.
- The runtime owner added ordinary control `tessera serve` and research
  `python -m tessera_agent.research_service` owner-composition wiring, explicit
  owner-config/digest flags, versioned JSON shapes and a pinned Python factory
  contract. The documentation shows only placeholder examples. After three
  adversarial findings (binding equality, fail-safe resource teardown, and
  nonblocking FIFO rejection) were repaired, the runtime handoff reports 54 distinct
  passing cases across retained gates—not 54 cases in one full gate. The combined
  seven-file source revision is
  `fdd464b3713f9795a2cdccf1ad4dcb0b60816ab67b06374b79f11e84fe52f33c`; focused
  reviewer confirmation remains pending. Synthetic fixtures do not prove a real signature trust
  root, loaded process, private owner adapter/resource handles, provider identity or
  an installed runtime. Legacy v1 remains strict default; v2 is explicit opt-in.
- The targeted public-doc privacy pass replaced local absolute paths, private
  addresses and ignored receipt/worktree links with portable configuration
  placeholders or labelled local-record IDs. Historical facts and required docs
  were retained; IDs are not distributed evidence. The package audit originally
  reported 58 metadata-pattern rows (32 path, 19 receipt-reference, six private-LAN,
  one header-shaped negative-test fixture), not 58 credentials. The header-shaped
  negative-test fixture and all source/test/script findings were left untouched.
  This docs pass did not rerun the package-wide scan, binary PDF/PostScript review,
  or the independent source/redistribution audit; no license or file-deletion choice
  is implied.
- `SECURITY.md` and the release checklist now describe the reporting endpoint result
  accurately: an authenticated API read returned HTTP 404 and feature availability/
  enablement remains unknown, neither enabled nor disabled. The maintainer must
  verify an available private channel before accepting sensitive reports. No API
  retry, login, settings change, visibility change, or publication occurred.
- The candidate publication audit's documented Dockerfile omission was repaired:
  the source builder allowlists only `deploy/remote-agent-platform/docker/Dockerfile`
  (with adjacent `.dockerignore` selected by the normal tree rules), and the three
  targeted tests cover allowlist/exclusions, symlink rejection, and reproducible
  archive/wheel parity. The parent reports those tests passed. A later independent
  credential-basename review also confirmed its P2 repair in builder SHA-256
  `af3fc2d606cd805a5bbe24bea88a53ff1c31b3899ada8e7c24a033aae1e19958`; its 83-case
  test evidence is separate from the three Dockerfile-focused tests. This is not a
  final package inventory or candidate gate. The README, MIT contribution
  policy, and required release docs were preserved. PDFs/PostScript require explicit
  rights/metadata review; they were not removed or silently treated as cleared.
- Before the first read-only public-doc review, all 122 changed Markdown documents'
  405 local-relative links were checked, including fragments: zero broken targets
  after replacing five stale/incorrect references with valid destinations. The earlier
  audit's 58 metadata-pattern rows were resolved within the reviewed text scope, not
  by a universal secret scanner; PDF/PostScript embedded metadata and redistribution
  rights remain unknown and no tracked file was deleted.
- Remaining live acceptance includes authentic owner/private credential registration
  through an approved owner path, genuine planner/critic reasoning, visible human UI
  review, restart during actual workload/training and crash boundaries, cancellation
  settlement, independent-host/off-host and laptop-off execution, native device
  selection, and scientific validity. Local protocol/Job results do not close these
  journeys. Next: collect runtime/reporting-owner handoffs, finish public-doc
  convergence, then perform the requested independent adversarial review before a
  final inventory/build/gate. Do not claim release completion from this checkpoint.

## Intent 11 — newcomer alpha journey and documentation checkpoint (2026-10-08)

- Added a single public [local macOS alpha quickstart](quickstart.md), linked from
  the top of the root README and desktop README. It follows the observed component
  boundary: Python service setup, separate Electron install/launch, native Connect,
  project/design, review/export; it distinguishes design from approval/grant/dispatch
  and links the existing operator procedure instead of duplicating platform manuals.
- The private native receipt `alpha-journey/initial.imfrzi1t/HANDOFF.md` records
  successful local init/readiness and a genuine Electron disconnected-screen capture.
  It does **not** evidence the connected project/design/results/export journey. The
  actual OS accessibility query was denied with native `-25211` in session
  `ses_ef8b63612ffe42mteiN1Ar34K6`; no rule/issuer identifier was exposed, no retry
  or permission change was made. The real owner-controlled picker/connection remains
  the human next step; credential content is not in this journal or public docs.
- The desk-side renderer repairs have distinct reported evidence: 130 renderer cases
  in the offline P2 handoff, plus 51 separate unchanged connection/publication-boundary
  cases in the earlier P2 handoff. These are local isolated DOM/API tests, not one
  181-case gate or native connected-flow proof. A focused source review retained the
  existing small publication blocks because their validation seams and error behavior
  differ; no abstraction rewrite was justified. Host Node 22.2.0 remains below the
  declared Node 22.12.0 minimum; no install/upgrade was attempted.
- No service/API/provider/credential/Job or candidate artifact was changed by this
  documentation checkpoint. Historical `c77` remains historical; no new freeze,
  build, native picker/save, connected UI, or experiment execution is claimed. Broader
  owner, generated-reasoning, off-host/device, production and scientific acceptance
  remains outstanding as recorded above.

## Intent 11 — simulator-connected alpha flow and baseline results (2026-10-08)

- The bounded current-renderer simulator used the real authenticated local control
  backend over HTTP and a narrowly scoped test platform shim. It created a project,
  experiment design and separate study, then retrieved the actual 2,875-byte study
  ledger. The destination chooser was simulated; this was not native macOS picker
  or save-dialog acceptance.
- A separately authorized normal Docker Desktop Kubernetes CPU baseline completed
  its one-job allowance successfully, with eight accepted execution-lease renewals.
  The actual 1,393-byte result was joined to its attempt, artifact and measurement
  records. Test-set MSE
  was 131.0623 and MAE 9.8565 over 1,667 rows; these are operational measurements,
  not scientific validation. The output remained late, quarantined and unclassified.
- In the simulator, the current renderer loaded the existing operator-recorded
  retention/custody metadata, safely handled a real missing-artifact 404, previewed
  the exact released output, retrieved its exact bytes through the simulated
  destination, created one reproduction-bundle **record**, and restored the same
  records after disconnect/reconnect. The simulator did not submit another human
  disposition or custody release. Bundle-record export is not a filesystem bundle;
  full experiment-chain JSON export remains API-first. Study-ledger JSON is separate
  from execution output.
- These results supersede the earlier checkpoint's statement that connected
  project/design/results screens were wholly unverified, but do not establish the
  native macOS Connect/credential-picker/save-dialog journey: an earlier actual
  Electron capture was disconnected, and simulator platform interactions were
  explicitly simulated. Node 22.2.0 remains below the declared 22.12.0 npm minimum,
  so a newcomer install on a supported Node version is unverified. This lane's
  one-job allowance is spent; no further job was run or needed for this
  documentation.
  No source change, candidate freeze or c77 relabeling occurred. The updated
  [quickstart](quickstart.md) describes the screen actions and these evidence limits.
