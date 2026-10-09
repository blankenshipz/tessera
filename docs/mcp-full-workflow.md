# Governed MCP workflow and off-host runbook

Supported transport: official Python MCP SDK **1.26.0**, session-based protocol
**2025-11-25**, **stdio**. The existing `mcp` extra already pins this dependency;
no dependency changes are required. This is not stateless MCP or an HTTP MCP server.
Install beside the remote agent using `pip install '.[mcp]'`, then configure its
MCP subprocess command as `tessera-mcp`. Stdio belongs to the agent process, not
the desktop; the agent host contacts the independently running HTTPS control API.

## Runtime references

Supply `TESSERA_CONTROL_URL`, `TESSERA_PROJECT_ID`,
`TESSERA_AGENT_DESTINATION`, and `TESSERA_AGENT_TOKEN_FILE` (owner-only regular
file, no symlink). Optional `TESSERA_CONTROL_CA_FILE` references public CA trust.
Use an enrolled agent credential, never an owner token. Each HTTP call reopens
the credential file for secure replacement. Remote HTTP and redirects are denied.
Do not put credentials in MCP arguments, scenarios, process arguments or logs.

Context is disclosed by separately authenticated humans, not created/disclosed by
the agent. Existing context listing/reading and task-plan proposals remain available.
An agent can propose project/study/experiment creation **inside its enrolled project**;
it cannot create or enroll itself into another project. Human promotion is outside MCP.

## Authority workflow (frozen backend interface)

`tessera_request_authority` accepts typed `AuthorityBounds` from
`docs/agent-authority-interface.md`. Requesting creates no approval. Humans issue
the grant using their separately authenticated HTTP/operator session.
`tessera_authority_status` reads the grant; `tessera_consume_grant` accepts the
closed discriminated union `attempt:admit`, `attempt:dispatch`, `attempt:status`,
`execution:cancel`. It has no arbitrary method/path/body proxy. Scope, tenant,
destination, model, expiry, revision/epoch, budget and immutable binding checks
belong to the backend; MCP annotations are hints, not authority.

Persist exact arguments and idempotency key before every mutation. Reconnect
with the same enrolled identity, destination, arguments and key. No automatic
retry occurs. An ambiguous transport outcome must not create a new key. Revoked
or expired authority may deny even replay; status reads do not renew authority.
Caller model provenance is attested, not cryptographically verified.

## Supplemental scoped HTTP contract

The adapter's closed typed calls are implemented by the converged backend in
`agent_research.py` / `agent_authority_api.py`. HTTP denial is never substituted
for successful acceptance. Human promotion/issuance stays outside the adapter:

| Route under `/projects/{enrolled_project_id}` | Method | Typed body | Required scope |
| --- | --- | --- | --- |
| `/agent-workflow-proposals` | POST | `ResearchProposal` | `proposal:create` |
| `/agent-observations` | POST | `Observation` | `observation:create` |
| `/agent-review-requests` | POST | `ReviewRequest` | `review:request` |
| `/agent-notebook?after_sequence=0&after_event_cursor=0&limit=20` | GET | none | `notebook:read` |
| `/agent-notebook/export` | GET | none | `notebook:read` |

Wire schemas live in `src/tessera_mcp/mcp_workflow.py` (do not import MCP SDK
into backend core; mirror or move schemas by coordinated integration).
All POST routes require `Idempotency-Key`, real agent bearer identity and
`X-Agent-Destination`. No caller-supplied organization or actor is accepted.
Proposals carry discriminated `project`, `study`, or `experiment` and
`AgentProvenance`; study/experiment bodies reuse existing strict design schemas.
They create immutable proposals, **not canonical records**, and must return `id`,
`revision`, `authority_effect: "none"`, and caller-attested provenance.
Observations reference an authorized attempt revision and artifact IDs, carry
bounded summary and attested provenance, and cannot mark execution verified or
pretend to be `human_reported_observation`. Review requests pin authorized subject
type/ID/revision; they neither review nor accept it. Reads return only redacted
destination-authorized records with completeness semantics, never project-wide
raw context or undisclosed artifacts. Mutations must atomically persist state,
event and idempotency; the backend must validate referenced resources and artifact
disclosure, and deny withdrawn, stale, wrong-tenant or wrong-destination records.
Notebook/export responses are bounded at 2 MiB by the adapter. Continue using
**both** `next.after_sequence` and `next.after_event_cursor`; `tessera_notebook`
names its sequence cursor `offset` for backward compatibility, but it is not a
row offset. `tessera_export_notebook` exposes both cursor names, a bounded limit,
and `max_bytes` (1024–2097152). Pages retain completeness and coverage semantics.

## Verification

Default `tests/test_mcp_workflow.py` uses synthetic HTTP transports only. It checks
closed schemas, explicit denial, identity/key preservation and typed tool inventory.
`scripts/check_mcp_control.py` starts its own real control service and real SDK
stdio subprocess clients. It checks curated context and proposal reconnect plus
denials, with temporary local state and no inference. It is not full workflow proof.

For a configured real installation:

```sh
python scripts/check_mcp_full_workflow.py --probe
python scripts/check_mcp_full_workflow.py --scenario /private/path/workflow.json
```

To exercise the entire protocol/control boundary in owned temporary loopback state:

```sh
python scripts/check_mcp_workflow_local.py
```

This generates its own private credentials and uses human HTTP/ordinary workspace
CLI for approved **synthetic** resources, plus worker HTTP for package preparation.
It never starts a worker executor, contacts a cluster/provider, runs inference, or
mutates existing installation state. Dispatch is queue admission, cancellation is
`cancel_requested`, and observation remains caller-attested. It is real MCP/HTTP
integration proof, **not** real cluster execution, reasoning, or laptop-off proof.
The owned control subprocess and temporary state are cleaned up at exit.

Each scenario step is `{ "tool": "tessera_consume_grant", "arguments": {...} }`.
Optional `denied: true` requires an MCP error. `same_as: 0` compares response `id`
with step 0 (optional `identity_field` selects another stable response identifier).
Every step uses a newly initialized real subprocess/session, so identical-key
replay crosses an actual disconnect. At most 64 calls; no inference is invoked by
the harness itself. Scenarios reference prepared bounded records/grants; human
issuance happens separately and must not be embedded as an agent tool call.
Do not execute dispatch until the authorized runtime/provider scope is configured.

Full acceptance requires a real scenario containing context read, research proposal,
authority request, separately human-issued grant consumption/admit/dispatch/status/
cancel, observation, review request, notebook and export, same-key replay and denied
expansion. Probe/tool discovery, mocks, or the existing proposal harness do not
satisfy that journey. Independent-host/laptop-off proof remains separate.

### Initial revision-1 worker evidence (2026-10-04; superseded blockers)

- Targeted adapter/workflow suite: 38 passed; subsequently added proposal/agent-
  observation/review schema test: 1 passed (16 deselected). SDK emits one
  `IncompleteFieldDefinitionWarning` for its `lifespan` settings forward reference.
- Scoped Ruff lint and format checks and whitespace check: passed.
- Initial `scripts/check_mcp_control.py` real subprocess/HTTP run: passed curated
  proposal/context/reconnect and denial/revocation/expiry behavior; no execution
  or inference.
- After adding the standalone harness subprocess/reconnect scenario, latest live
  run stopped at control readiness, before protocol calls. Fresh temporary
  `initialize_state` / `create_app` readiness returned **503 `schema_mismatch`**
  during concurrent backend schema integration. The new standalone scenario is
  implemented but that checkpoint version had not yet passed live.
- Full research/authority/observation/review/notebook/export acceptance: pending
  backend supplemental routes, enrollment scopes, service readiness and prepared
  human-issued execution/authority bindings. Resume the same worker for that run;
   never reinterpret the initial proposal-only pass as full-workflow proof.

### Resumed revision-1 worker evidence (2026-10-04)

After backend convergence, `.venv/bin/python scripts/check_mcp_workflow_local.py`
passed **29 real SDK subprocess tool calls**, with a freshly initialized session
for every call. Full ordered receipts (pass unless marked denial):

1. Credential status; list context; read disclosed context.
2. Study proposal; project proposal; experiment proposal (all no authority effect).
3. Request authority; list own requests; read separately human-issued grant status.
4. Consume admit; reconnect and identical-key admit replay (identical response).
5. Consume attempt status after synthetic worker HTTP package preparation.
6. Dispatch expanded target **denied**; exact-target dispatch; reconnect replay.
7. Status after dispatch; caller-attested observation; separate human review request.
8. Consume status under a wall-clock-expired authority grant **denied**.
9. Cancel; reconnect and identical-key cancel replay (`cancel_requested`).
10. Notebook page; dual-cursor continuation (no repeated event cursor); export with
    expected request/issue/consume/proposal/observation/review control events,
    complete recorded metadata snapshot and explicit non-verification semantics.
11. Nonexistent human approval tool **denied**; arbitrary forwarding union **denied**.
12. Human revokes authority separately; original admit replay **denied**; export
    joins revocation event. Wall-clock-expired credential status **denied**.

Human proposal promotion and grant issuance under the agent credential were also
denied by real HTTP; human setup and agent tool sessions used distinct credentials.
No human credential entered an MCP subprocess. Initial harness setup repairs:
ordinary CLI uses its entrypoint rather than an inert module invocation, and
authority expiry contains execution lifetime while staying inside credential expiry.

`.venv/bin/pytest -q tests/test_mcp_workflow.py tests/test_mcp_adapter.py`:
**46 passed**, one unchanged SDK settings warning. Scoped Ruff lint/format passed.
The final `scripts/check_mcp_control.py` run also passed, including its separate
standalone `check_mcp_full_workflow.py` reconnect scenario. The combined command
timed out after full-workflow success; the old harness was rerun alone successfully.
Process inspection found no owned fixture processes remaining.

Remaining: installed-wheel variant after final tree quiescence, actual cluster/
reasoning integration and independent-host/laptop-off evidence are parent-owned
acceptance. The schema readiness blocker and missing supplemental routes reported
in the historical checkpoint are resolved for these local protocol runs.

## Revision 3: pinned OpenCode V2 research-harness binding

The earlier real SDK/control fixture does **not** establish that a research model
discovered or used MCP or a curated skill in OpenCode. Revision 3 expands `full-mcp`
to that actual harness boundary. This source lane implements binding artifacts;
consumer integration and live discovery remain unverified.

| Check | User-facing journey | Components / lane | Required proof | Prerequisites |
| --- | --- | --- | --- | --- |
| `full-mcp` (source subset) | Planner/critic receives only admitted tools and curated skill | `harness_binding.py`, governed skill, isolated tests; MCP source lane | Synthetic deterministic config/pin/denial fixtures | Public operator-reviewed model/catalog/skill pins |
| `full-mcp` (live; deferred to integration) | Real research harness discovers exact permitted catalog and selected skill, then performs typed workflow/reconnect | Dedicated OpenCode V2 harness, runtime consumer, stdio adapter | Actual catalog, loaded skill, effective config, account and principal observations before inference | Dedicated authorized included-OAuth account/runtime; installed Python+MCP extra; mapped agent credential and public CA; pinned isolated workspace/config |

### Interfaces (no consumer edits in this lane)

New module: `tessera_mcp.harness_binding`.

- `generate_binding(...) -> HarnessBinding` is a pure artifact generator. Inputs
  are public project/subject/destination/origin identifiers, explicit absolute
  installed-Python/workspace/credential-directory/skill-source/CA references, role,
  finite exact tool/model allowlists, step limit, reviewed SDK `tools/list` JSON
  catalog and skill bytes, plus their independently approved SHA-256 digests.
  It also requires the approved harness version, public OpenAPI schema SHA-256
  and installed Tessera build SHA-256; SDK version is fixed to 1.26.0.
- `generate_from_mapper(environment=..., **arguments)` resolves **directory metadata
  only** via the existing `tessera_agent.service_startup.credential_root`. It cannot
  override that root, calls no credential reader, and forwards no provider/human/
  harness token environment. The generated MCP environment references exactly
  `<manager-root>/agent-token`; no token value enters config, manifest or skill.
- `HarnessBinding.config`, `.manifest` and `.skill` are immutable bytes;
  `.digest` is the manifest digest. The caller must stage these as **new pinned
  run artifacts**, never write global config or overwrite unrelated project config.
  Stage the skill at `<skill_source>/tessera-governed-research/SKILL.md`.
- `catalog_sha256(catalog)` pins the complete reviewed JSON schemas/descriptions/
  annotations, sorted by tool name; missing, duplicate or unexpected tools block.
- `verify_binding(...)` compares the admitted manifest digest, generated/effective
  config, selected skill bytes, discovered catalog, public principal/destination/
  required-scope/expiry status, and selected model/account observations. It neither
  accepts changed harness/version/schema/build/SDK observations nor treats source
  configuration as proof that those installed versions are present. It neither
  discovers nor authenticates a harness. Echoing fixtures into it remains synthetic
  evidence; its OAuth boolean is a **caller-supplied verified observation**, not an
  account-verification implementation or a credential.

### V2 configuration contract

Sources consulted (V2 only; no V1 schema inference):

- <https://opencode.ai/v2/docs/config>
- <https://opencode.ai/v2/docs/mcp-servers>
- <https://opencode.ai/v2/docs/permissions>
- <https://opencode.ai/v2/docs/policies>
- <https://opencode.ai/v2/docs/agents>
- <https://opencode.ai/v2/docs/skills>
- <https://opencode.ai/v2/docs/providers>

The generated config uses `mcp.servers.tessera`, local installed Python stdio,
`protocol:"legacy"`, `codemode:false`, explicit startup/catalog/execution
timeouts, `disabled:false`, V2 `agents` and ordered `permissions`, and a local
`skills` array. `$schema` is retained for editor integration; that URL was **not**
used as the V2 field-shape authority. Root and selected-agent rules deny all first,
then permit only the exact curated skill ID and approved native MCP actions.

OpenCode prefixes SDK tool names with the server name: permission action
`tessera_tessera_consume_grant` is correct for SDK tool `tessera_consume_grant`.
No shell/read/edit/web/network/subagent/Code-Mode permission is granted. Hard
`experimental.policies` deny other provider IDs and ambient permission actions,
with exact exceptions. Broader operator/Console denials are never bypassed.

V2 documents `provider.use`, **not** a `model.use` policy or an included-OAuth-only
config flag. Therefore config cannot alone enforce a model allowlist or billing
mode. The manifest carries the exact approved model IDs, and admission rejects
different/unknown model/account observations. The approved dedicated runtime must
establish included-ChatGPT OAuth (not API-key fallback), strip ambient provider
keys, respect quota/budget fences, and supervise outputs; source fixtures cannot
prove that. No OAuth store is read/copied and no fallback provider is configured.

OpenCode merges ancestor/global configs and skill sources. Effective merged config
must match the admitted pins; unexpected tools, skill overrides, broader rules or
plugins require blocking review, not silent merge. `verify_binding` currently
expects the exact documented config projection; consumer integration must derive
that projection from supported V2 public APIs and also check any effective hidden
maintenance/model paths before inference. Do not invent an API schema or remove
broader policy to force a match.

### Remaining live discovery and consumer prerequisites

The current `remote_reasoning_runtime.validate_profile` allows only exact MCP
permission actions after its deny rule; it rejects `skill` permissions. Runtime
consumer work must deliberately support the exact curated skill exception and
pin the generated configuration/skill/catalog, without allowing arbitrary skills.
That existing file was not edited by this source lane. The mapper's existing child
environment also does not automatically install this new project config in the
separate OpenCode service; that explicit startup/consumer integration is pending.

Before declaring revision-3 `full-mcp` passed, use an authorized dedicated installed
V2 harness to discover the exact catalog, selected skill and public credential
status; record config/skill/catalog/principal/TLS/account pins and actual outcomes.
Then exercise typed proposal/request/consume/dispatch/status/cancel/observe/review/
export and same-key reconnect. Keep human issuance/review outside the agent session.
Stdio beside an off-host agent is supported; no HTTP MCP server is provided.
No host deployment, inference, authentication discovery, credential copying or
global config mutation was performed by this source-only assignment.

Revision-3 source checkpoint: `.venv/bin/pytest -q tests/test_mcp_harness_binding.py`
**44 passed (2.79s)**. Scoped Ruff lint and format checks and diff whitespace
check passed. Tests use synthetic catalog/model/account observations, real temporary
directory metadata for the fixed mapper and the curated source skill bytes; none
establish live OpenCode discovery, real account admission or actual inference.

## Revision-3 generated-public-output MCP family

Backend contract: [generated-public-output-interface.md](generated-public-output-interface.md),
schema 53. This is a distinct public-research output registry, not worker Job
artifacts, execution receipts or inference verification. `src/tessera_mcp/generated_outputs.py`
adds three exact typed SDK tools to the stdio server:

| SDK tool (OpenCode action adds `tessera_`) | Request fields / route | Existing explicit rights |
| --- | --- | --- |
| `tessera_register_generated_output` | `payload:RegisterGeneratedOutput`, `idempotency_key`; POST enrolled project's `/generated-outputs` | Sender `context:read` + `provenance:append` **and** separately human-issued generated-output grant |
| `tessera_generated_output_metadata` | `output_id`, `disclosure_id`; GET `/generated-outputs/{id}/metadata?disclosure_id=...` | Current human-issued recipient disclosure and recipient `context:read` |
| `tessera_read_generated_output` | `output_id`, `disclosure_id`; GET `/generated-outputs/{id}?disclosure_id=...` | Same current recipient disclosure/read fences; includes approved bounded bytes |

Registration reuses the **backend** closed request model: `grant_id`,
`expected_revision`, `pins`, `source_event_id`, `source_sequence`,
`output_kind` (`planner_proposal` or `critic_review`), `visibility:"public"`,
`sha256`, `content_base64`. `pins` carries exact model/run/session/harness-instance/
context ID and digest, skill/config digests, and nullable real task/operation IDs.
No human admission/disclosure/revoke fields, arbitrary methods/paths, host file
names, raw prompts, hidden reasoning or tool payloads are accepted. Public-source
admission remains the human/runtime responsibility; text/hash checks cannot prove
what produced the bytes.

The adapter locally enforces nonempty UTF-8, no NUL, at most **65536 decoded bytes**,
strict base64 and exact SHA-256; responses are capped at **128 KiB JSON**. Byte reads
also verify actual size/hash/content-digest and enrolled project. Metadata must not
contain bytes. The real agent bearer and destination still go through the existing
`ControlClient`; tenant/grant/context/pin/budget/revision/current lifetime checks
remain backend authority. Registration is not disclosure, including for its author.
Grant revocation denies new writes and replay; current human recipient disclosure
withdrawal denies reads. No grant/disclosure/revoke MCP tools were added.

The pinned catalog now has **18 tools**. `PERMITTED_TOOLS`, scope generation and the
curated governed skill include only these three additions. Full binding registration
now requires the existing ninth enrollment scope `provenance:append`; a read-only
subset still requires only `context:read`. Old 15-tool catalog pins and old skill/
config/manifest digests must fail admission; regenerate and separately approve them
rather than silently accepting capabilities. The fixed credential mapper is unchanged.

Focused proof uses actual MCP SDK `ClientSession`/JSON-RPC memory sessions plus
actual FastAPI ASGI and temporary SQLite, with separately authenticated synthetic
human setup, grant issuance, recipient disclosure and revoke. Outputs and model/
account claims are fixtures. This is **not** installed-harness discovery, real
generated reasoning, external stdio/host proof or ordinary live harness admission.
The new event asserts content-free `generated_output.registered` identity without
fabricated execution artifacts; the full notebook join remains the provenance lane.

The SDK inventory assertion in `tests/test_mcp_workflow.py` is updated to 18.
Parent-owned `scripts/check_mcp_control.py:178` still asserts 15 tools and needs an
inventory-only update before its standalone rerun; this source assignment does not
own that script. Existing installed/live receipts remain historical, not upgraded
to the expanded catalog or generated-output workflow by source tests.

Continuation checks: the initial four-file targeted run passed **102 tests**
(11 generated-output, 45 repeated binding, 46 existing MCP adapter/workflow).
After adding author/recipient, wrong-destination and exact-64-KiB coverage, the
final generated-output-only run passed **13 tests (27.48s)**. The earlier 44-test
binding receipt is superseded by the 45-test expanded-catalog run. Scoped Ruff
lint/format (six files) and diff whitespace checks passed. Three existing SDK/
Starlette settings/deprecation warnings remain. No inference or installed/live
harness admission was attempted; full notebook projection/join is not claimed.
