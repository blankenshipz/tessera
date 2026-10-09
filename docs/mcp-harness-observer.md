# Dedicated OpenCode V2 public harness observer (intent revision 3)

`src/tessera_mcp/harness_observer.py` implements the safely observable REST boundary.
It **does not complete admission** on the currently documented V2 surface. A
connected MCP server, separate MCP client, source manifest or fixture account flag
must never substitute for the research harness's actual discovered/selected state.

## Evidence sources inspected

Public documentation and selected JSON schemas, not a running coding service:

- <https://opencode.ai/v2/docs/api>
- <https://opencode.ai/v2/openapi.json>: selected paths/schemas `ServerInfo`,
  `Mcp.Server`, `Mcp.Status.Connected`, `Skill.Info`, `Session.Info`,
  `Config.Entry`/`Config.DocumentEncoded`, `Integration.Info`, `Connection.Info`.
- <https://opencode.ai/v2/docs/build/plugins>: documented `ctx.tool.list()`,
  `ctx.skill.list()`, metadata-only `ctx.integration.connection.active()` and
  per-request session hooks. No token resolution, provider HTTP/WebSocket headers,
  or private OpenCode internals were accessed.

These are V2 contracts; the V1 editor config schema was not used. The observer
checks the separately approved manifest's exact harness version and canonical
OpenAPI digest against actual GET responses before reading other public state.
Tests use minimal synthetic versions/schemas and do not pin an installed version.

## Supported interfaces

`V2PublicHarnessObserver(binding, harness_endpoint, control_endpoint,
research_origin, coding_origins, session_id, coding_pids=(), clock=...)` accepts
publicly identified, ordinary **authenticated** endpoint adapters with `.origin`
and `.call("GET", path)` (for example existing installed `JsonEndpoint` objects
from the trusted credential mapper). It does not construct/read/authenticate them,
inspect headers, copy credentials, start services or use automatic CLI discovery.
Supplying fixture adapters is synthetic evidence, not authentication proof.

The endpoint origins must match the separately approved research/control origins.
An explicit coding-service exclusion inventory is required; known origin or PID
reuse is denied. Unknown alias/process identity is **not** promoted to a dedicated
instance identity, and remains an admission gap. A dedicated research service is
required; the coding service must not be borrowed to satisfy these checks.

`inspect_public() -> PublicObservationReport` performs bounded GETs only:

| Public route | Actual measurement | What it does NOT prove |
| --- | --- | --- |
| `/openapi.json`, `/api/info` | Canonical schema digest, version, process PID | Stable instance UUID, installed Tessera build, launched stdio SDK build |
| `/api/location?location[directory]=...` | Requested research workspace readback | Filesystem/process isolation or safe merged config |
| `/api/mcp?location[directory]=...` | Exactly one connected `tessera` server | Harness-discovered tool schemas or executable model-request snapshot |
| `/api/skill?location[directory]=...` | Unique registered curated skill ID and content-body digest | Raw `SKILL.md` bytes/frontmatter, selected/activated skill in the model context |
| `/api/session/{session_id}` | Current session/agent/location and exact OpenAI model reference | Actual account/route/entitlement or active inference |
| Tessera `/external-agent/status` | Current identity, project, destination, required scopes, expiry | Human approval, provider account admission or inference authority |

Reads are bound to the approved workspace/session and discard arbitrary session
metadata and prose. Registered skill content is only hashed; body text is not
returned or logged. Source IDs/model/account facts are not copied from the manifest
into observed fields. Response/schema/identity/location/model/scopes/expiry drift,
extra MCP servers, injected catalog fields and upstream failures halt with fixed
sanitized reason codes. Public-response JSON is bounded; no automatic retry occurs.

The callable observer is the `ResearchConsumer.observation_reader()` port. It
collects these fresh observations, then raises `ObservationBlocked`; it does **not**
return incomplete `verify_binding` kwargs that callers could fill with manifest
echoes. `PublicObservationReport.admission_fields()` likewise never admits a run.

## Explicit blocking gaps

1. **Effective config:** `/api/config` returns ordered config documents/discovery
   entries, not a safe final effective runtime projection. Documents/provider
   fields may contain credentials. This observer does not call that endpoint or
   merge/serialize potentially secret-bearing config into a journal.
2. **Full harness catalog:** `/api/mcp` has server name/status/integration ID only;
   no `tools/list` schemas. `/api/mcp/resource` is not a tools catalog. Connected
   status and a second stdio client's catalog are insufficient.
3. **Selected skill:** `Skill.Info.content` is registered instruction content, not
   raw file bytes. Registration is not model-context activation. No raw-file or
   loaded-content digest is reconstructed from a source manifest.
4. **Stable instance:** `ServerInfo.pid`, URLs and temp path are not the admitted
   opaque logical `harness_instance_id`; none is fabricated from a PID or origin.
5. **Build/SDK:** server version/schema alone cannot identify the installed Tessera
   package or actual MCP child SDK. Observer-process imports would not prove what
   the research harness launched.
6. **Included OAuth:** `Integration.Info.connections` describes available methods;
   it does not establish which account/route this model request uses. The HTTP
   API has no active-connection equivalent with independently established included
   billing/no-key-fallback/entitlement data. The plugin's metadata-only active
   connection can distinguish OAuth/key/env, but OAuth alone does not prove the
   included-ChatGPT billing route. No account-admitted boolean is manufactured.

There is no call to `/api/credential`, credential resolution, raw provider
configuration, provider request headers, filesystem, message/context/log, prompt,
generation, auth-connect, MCP connect/disconnect or config mutation routes.
Unknown telemetry stays unknown; it is not quota exhaustion or permission for paid
fallback. This boundary never triggers inference.

## Shipped plugin continuation (source only)

A Tessera-shipped in-harness plugin now observes effective registered MCP tool
schemas through documented `ctx.tool.list()` and captures a context-hook request
snapshot. The parent authorized these additional paths; they are implemented:

- `plugins/tessera-harness-observer/index.ts`
- `plugins/tessera-harness-observer/package.json`
- `plugins/tessera-harness-observer/README.md`
- `tests/test_mcp_harness_observer_plugin.py`

The plugin exposes only a version-pinned, bounded, nonce/session/location-bound
projection through existing V2 RPC; no tool executors, messages,
reasoning, request headers, token resolution or config credentials. Tool registry
metadata is not automatically the original raw SDK catalog: define and separately
approve its exact canonical representation rather than silently coercing it into
the old SDK catalog hash. Loaded skill content versus raw-file pins also requires
an explicit reviewed contract, not a guessed normalization.

See [the plugin protocol and factory recipe](../plugins/tessera-harness-observer/README.md)
for the exact endpoint, constructor, authentication prerequisite and source-only
installation instructions. `V2PluginHarnessObserver` joins the public REST facts
with this projection through a strict authenticated, no-redirect HTTP adapter.
It does not return verifier admission fields while remaining gaps are unresolved.

`V2AttestedHarnessObserver` now implements complete production **consumption** of
an independently authenticated issuer's guard measurements. Its exact closed
protocol, trust pins, freshness/revocation/account/hook verification, synthetic
production-path results and field-specific unsupported producer APIs are documented
in the [plugin README](../plugins/tessera-harness-observer/README.md#complete-production-admission-consumption-contract).
It does not fabricate an installed guard: authenticated issuer claims are not
independent proof of truth, and the dedicated guard must actually measure/enforce
the stated final runtime/account boundaries. Plain plugin observation stays partial.

Plugin implementation alone does **not** solve missing instance/build/SDK trust or
included-route entitlement/accounting. Those require actual supported public
observations/attestation from the dedicated installed platform or a precise reviewed
extension. Do not add pretend API fields, relax admission, or write external
OpenCode source/config to force success. Broader operator/runtime denials remain.

## Source checkpoint

Plugin continuation: **16 transport/auth/private-route cases passed**; after a
TypeScript spread-type repair, **14 compiled-plugin cases passed**. Following
bounded description handling and RPC/Python composition additions, **24 compiled
and protocol cases passed (8.22s)** with the 16 unchanged transport cases deselected.
The plugin is compiled against mock SDK declarations and executes mock SDK hooks;
this is not a real installed-SDK typecheck or live admission. No installed calls,
service/auth-store/environment/host operations, inference or external writes occurred.

`.venv/bin/pytest -q tests/test_mcp_harness_observer.py`: **19 passed (0.20s)**.
Fixtures cover supported public projections, schema/version/model/session/workspace/
principal/scope/expiry drift, coding-origin/PID reuse, missing routes, extra servers,
injected catalog data, redacted upstream errors and rejection of account booleans.
Scoped Ruff lint passed. No unchanged binding/output suite was repeated; no full
suite, service, credential/auth-store read, installed harness call, inference,
provider/host/SSH operation or external source/config write was performed.

Ordinary factory integration remains blocked until the above fresh complete
observations exist. The safe observer is not advertised as a finished planner/
critic runtime or live `full-mcp`/`remote-runtime` acceptance.
