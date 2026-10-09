# Tessera dedicated-harness observer plugin

Source-only package, pinned to **`@opencode/plugin@2.0.18`**. No coding-instance
installation, config change, service start or inference has been performed.
The package is private and is not published. The existing observer tests compile
this shipped TypeScript and execute it with documented mock SDK interfaces; they
are **synthetic**, not a real-SDK typecheck or installed admission receipt.

## Supported V2 interfaces

Verified public contract:

- [Plugin RPC](https://opencode.ai/v2/docs/build/plugins/rpc/): `Rpc.define`,
  `ctx.rpc.register`, declared errors, registration disposal.
- [Plugin hooks](https://opencode.ai/v2/docs/build/plugins/): `ctx.tool.list()`
  after transforms; `ctx.skill.list()`; `ctx.session.get()`;
  `ctx.session.hook("context", ...)`; metadata-only
  `ctx.integration.connection.active(integrationID)`.
- [V2 OpenAPI](https://opencode.ai/v2/openapi.json): actual endpoint
  `POST /api/rpc/{rpcID}/{method}`, optional deep-object location query,
  request `{ "input": ... }`, response `{ "output": ... }`.

Read-only source inspection confirmed the public SDK package version, `Tool.Info`
projection, `SessionContext.tools` JSON schemas/model and `RpcCallContext` shapes.
There is no credential resolution, provider header access, filesystem read,
config read/merge, message/reasoning capture or separate MCP client.

## Operator installation recipe (not executed)

1. Obtain explicit human authorization for a **separate** research server, its
   paired/authenticated endpoint, project principal and bounded session. Do not
   borrow the coding server, coding credentials or any owner-token proxy.
2. Install this local package with exactly `@opencode/plugin@2.0.18` in that
   dedicated environment; independently verify the installed version and package
   digest. Package managers/dependency installation are not run by this lane.
3. Add the local plugin directory to the dedicated profile's V2 `plugins` array.
   Do not edit any global/coding configuration. Reviewed plugin options are:

   ```json
   {
     "dedicatedResearch": true,
     "workspace": "/approved/research/workspace",
     "sessionID": "ses_APPROVED_EXISTING_RESEARCH_SESSION",
     "agent": "tessera-planner",
     "skillID": "tessera-governed-research",
     "providerID": "openai",
     "modelID": "APPROVED_MODEL_ID",
     "integrationID": "APPROVED_OPENAI_INTEGRATION_ID",
     "permittedTools": ["tessera_tessera_status"]
   }
   ```

   The one-tool example is illustrative: the actual closed allowed native-name
   inventory must equal the human-approved binding's `permission_actions`, e.g.
   the full current 18 tools where authorized. No self-admission option exists.
   Options are expected constraints; they are **not observed authorization**.
4. Enforce the existing server authentication before exposing RPC. The OpenAPI
   endpoint does not itself attest server auth enforcement; separately verify
   unauthenticated access is denied. No unauthenticated sideport is created.
5. Regenerate/reapprove changed profile/package/catalog/skill pins. Keep billing
   and installed-runtime attestations separate from plugin options and source
   manifests. Complete the prerequisites below before ordinary factory admission.

## Protocol and Python factory port

RPC ID `tessera.harness.observer.v1`, method `observe`, protocol
`tessera-harness-observer-v1`. On the approved origin:

```text
POST /api/rpc/tessera.harness.observer.v1/observe?location%5Bdirectory%5D=ENCODED_WORKSPACE
{"input":{"protocol":"tessera-harness-observer-v1","nonce":"FRESH_RANDOM_NONCE","sessionID":"ses_APPROVED"}}
```

Response output is bounded to 128 KiB and schema-validated by V2 RPC. It contains
fresh registered MCP tool metadata `{name,description,inputSchema}`, the registered
skill **body** digest, current session/model, sanitized active connection metadata,
and (if a context hook has actually occurred) a local sequence/time/model/tool-schema
snapshot. Nonce/session/location are echoed and independently checked by Python;
they are not instance identity or provider execution receipts. Catalogs with missing,
extra or duplicate Tessera native names are denied; Standard Schema objects are not
pretended to be JSON Schema. Other native tools may exist in the registry; the
request snapshot is retained only if it contains precisely the permitted tools.

Canonical transformed catalog JSON is its own observed representation. It is not
silently relabeled the original MCP SDK `catalog_sha256`; approve a representation
contract before binding it into admission. The hook captures a point in plugin
order, not an attestation that later hooks did not alter the actual model request.
It never reads/stores system/messages/options, tool executors or raw selected skills.

Factory construction, without changes to runtime-owned files:

```python
from tessera_mcp.harness_observer import ObserverHttpEndpoint, V2PluginHarnessObserver

# Authorization header strings are supplied securely by the authorized mapper;
# never logged, entered in source, copied from the coding service or provider auth.
harness = ObserverHttpEndpoint(approved_research_origin, mapped_server_authorization)
control = ObserverHttpEndpoint(approved_control_origin, mapped_agent_authorization)
reader = V2PluginHarnessObserver(
    binding=approved_binding,
    harness_endpoint=harness,
    control_endpoint=control,
    research_origin=approved_research_origin,
    coding_origins=known_coding_origins,
    coding_pids=known_coding_pids,
    session_id=approved_research_session_id,
)
report = reader.inspect_public()  # safe partial measurements + explicit gaps
# reader() is the ResearchConsumer observation_reader port, but still blocks
# admission while the following complete trusted observations are unavailable.
# Close both endpoints at factory/service shutdown.
```

The HTTP adapter requires supplied Basic/Bearer server authorization, verifies TLS
normally, ignores environment proxies/auth/cert overrides, disallows redirects and
arbitrary routes, bounds responses and sanitizes all upstream errors. It does not
discover or read auth stores. Origin and known coding-process exclusions are checked;
aliases/unknown process identity remain unproven, not implicitly dedicated.

## Remaining admission prerequisites

No plugin-generated `admitted=true`/`included_oauth_verified` field exists. Even a
fresh OAuth connection ID/method is **not proof** of included-ChatGPT entitlement,
active provider route, billing mode or no paid fallback. Key/env/absent/needs-auth
connections remain explicit negatives; OAuth with unknown accounting stays unknown.

The installed platform must supply a separately trusted, version-pinned, origin/
session/enrollment-bound public observation or signed attestation for:

- safe effective config and final permission/route policy, without credentials;
- raw curated skill bytes and actual selected/activated content representation;
- stable installed harness identity (not PID, plugin nonce or a fabricated UUID);
- installed Tessera/plugin build and actual launched MCP child SDK identity;
- the actual selected OAuth account route, included entitlement and no paid-key/
  credit fallback;
- final model-request hook ordering and freshness, including post-hook drift.

The plain plugin reader still fails closed. The additional
`V2AttestedHarnessObserver` below consumes an independently authenticated complete
guard attestation. It implements verification, **not a fabricated producer**.
Current principal/scopes/expiry remain independently read from Tessera status.

## Complete production admission consumption contract

```python
from tessera_mcp.harness_observer import (
    GuardTrust,
    ObserverHttpEndpoint,
    V2AttestedHarnessObserver,
)

# guard_origin and all trust fields are explicit separately approved enrollment
# inputs from the human runtime factory, never plugin options or response fields.
guard = ObserverHttpEndpoint(approved_guard_origin, mapped_guard_authorization)
trust = GuardTrust(
    issuer=approved_issuer,
    trust_reference=approved_trust_reference,
    producer_sha256=approved_guard_implementation_digest,
    enrollment_id=approved_enrollment_id,
    instance_id=approved_stable_instance_id,
    principal_id=approved_actor_id,
    entitlement_reference=approved_public_entitlement_reference,
    entitlement_sha256=approved_public_entitlement_digest,
    policy_sha256=approved_no_paid_fallback_policy_digest,
)
reader = V2AttestedHarnessObserver(
    guard_endpoint=guard,
    guard_trust=trust,
    binding=approved_binding,
    harness_endpoint=harness,
    control_endpoint=control,
    research_origin=approved_research_origin,
    coding_origins=known_coding_origins,
    coding_pids=known_coding_pids,
    session_id=approved_research_session_id,
)
# reader() returns verify_binding kwargs + observed_harness_instance_id ONLY
# after complete validated fresh guard and independently read public observations.
```

The guard endpoint must be an explicitly approved **independent HTTPS origin**, not
the research or control origin. Authenticated HTTPS plus fixed issuer/trust/digest
pins establishes which approved issuer supplied a claim; it does **not prove the
claim is true**. No detached-signature format is claimed, no trust-on-first-use,
response-provided trust roots, cached admission or signature-only truth is accepted.
The factory closes all three endpoints at shutdown.

**Specified producer interface, not an existing OpenCode route:**
`POST /tessera/runtime-guard/v1/observe` on that approved guard origin. Its request:

```text
protocol = tessera-runtime-guard-admission-v1
nonce = fresh random 256-bit challenge
enrollment_id, binding_sha256, instance_id, session_id, research_origin
public_observation_sha256 = canonical digest of fresh Python public/plugin projection
```

The guard response is one closed object (unknown fields rejected), containing all
request fields unchanged and these additional fields:

- `issuer`, `trust_reference`, `producer_sha256`: match separately approved pins;
- `proof_id`, positive integer `revision`, `state`: the issuer's **current**
  enrollment/proof revision and revocation decision; only `active` can pass;
- `issued_at`, `expires_at`: UTC offset timestamps, issued no later than now,
  expiry strictly future, lifetime at most **30 seconds**;
- `measurements`: closed measured projection described below;
- `evidence`: exactly `config`, `catalog`, `skill`, `identity`, `account`, `hooks`.

Each evidence item has **exactly** `origin`, `mode`, `captured_at`, positive integer
`revision`, `value_sha256`. Only `mode="measured"` is accepted; `human_asserted`,
manifest/plugin-options echoes and unknown modes fail. Capture must be between
issuance and verification time; digest must match its measured value. Hashes use
UTF-8 JSON, sorted keys, compact separators, no NaN, normal Python ASCII escaping.
These labels authenticate the issuer's claimed provenance, not independent proof.

| Group | Required origin | Measured values/digest input |
| --- | --- | --- |
| config | `guard-effective-registry-export` | `effective_config` object (safe, secret-free actual runtime export) |
| catalog | `guard-harness-registry-export` | `catalog` with exact SDK `{name,description,inputSchema}` rows |
| skill | `guard-loaded-skill-and-artifact-readback` | `{selected_skill_base64,skill_body_sha256}`; raw approved bytes plus actual loaded body digest |
| identity | `guard-supervised-process-and-installed-artifacts` | `{harness_version,schema_sha256,build_sha256,mcp_sdk_version,principal_id,provider_id,model_id}` |
| account | `guard-selected-route-entitlement-and-billing-policy` | account object below |
| hooks | `guard-final-request-snapshot-and-plugin-chain` | hooks object below |

The measured catalog uses an explicit **sdk-native-name-map-v1** comparison: native
name is `tessera_` plus SDK name; descriptions/input schemas must match the fresh
in-harness plugin projection exactly. No extra keys, invisible schemas or separate
MCP subclient can substitute. The original full closed SDK catalog also passes the
existing `catalog_sha256` pin. Raw selected skill bytes, safe effective config,
installed build/schema/SDK and model are checked by the existing binding verifier.
No manifest value is returned as an observed field without a guard value/readback.

`account` has exactly `connection_id`, `method`, `provider_id`, `model_id`, `route`,
`entitlement_reference`, `entitlement_sha256`, `policy_sha256`, `policy`. Connection
ID must match fresh metadata-only `connection.active()`, with OAuth and no needs-auth
status. Provider/model match independently read session metadata. Route must be
`chatgpt-included`; entitlement ref/digest and effective policy digest match explicit
trusted enrollment pins. Effective measured policy must be exactly:

```json
{"allowed_auth_methods":["oauth"],"billing_routes":["chatgpt-included"],"on_unknown":"deny","on_exhausted":"stop"}
```

No account-admitted boolean is accepted. Only after all route/connection/entitlement/
policy checks does the local verifier produce its legacy compatibility field
`included_oauth_verified=True`; neither plugin nor guard can submit that flag.
Unknown account telemetry/route cannot pass merely because OAuth is selected.

`hooks` has exactly `chain`, `snapshot_sha256`, `final_guard_id`,
`final_guard_revision`, `registered_catalog_sha256`, `effective_config_sha256`.
Chain is ordered, unique, bounded; its final ID must be
`tessera-runtime-admission-guard`. Digests bind the fresh actual plugin request
snapshot, catalog and measured effective config. The snapshot timestamp must itself
be at most **30 seconds old** and not future; a fresh issuer response cannot refresh
old model-request evidence. Producer must enforce the final chain/route before
dispatch and atomically reject revocation/drift. A declared hook list is not a
measurement: producer must obtain final installed hook order at the dispatch boundary.

Every fence makes fresh REST/plugin observations and a new guard challenge; no
receipt is cached. Revoked state, stale/unknown fields, nonce/instance/origin/
principal/model/binding/digest/account/hook mismatch fail with a sanitized fixed
error. Authentication redirects and authorization failures never trigger retries
or fallback. Returned observations match `ResearchConsumer.observation_reader()`
and have already passed `verify_binding`; the consumer retains its own fence.

## Exact producer gaps and minimal handoff decision

The source verifier is complete for this protocol. **No installed producer or live
trust inputs were supplied or invented.** Public SDK source inspected at
`opencode-subscription/runtime/v2-current/source/packages/plugin` version **2.0.18**:

| Unsupported producer measurement | Exact public evidence | Required external measurement/integration |
| --- | --- | --- |
| Final hook chain/order and sealed request | `src/promise/session.ts:25-36` exposes session/model/system/messages/options/tools; public `session.hook` registers a hook, but provides no final hook-chain/order/seal API. Docs explicitly say later hooks see/change earlier hooks' output. | Dedicated runtime's trusted final-dispatch guard must export/enforce actual hook order and final immutable request digest. Placing a plugin last in config is not sufficient measured proof. |
| Secret-free final effective config | V2 `/api/config` returns `Config.Entry` documents/directory entries, not a final safe effective projection; domain transforms can subsequently alter state. | Approved runtime exporter must read final effective registry/permissions/route state and produce the exact reviewed safe config representation without provider/auth values. |
| Raw skill bytes + selected activation | `Skill.Info`/`ctx.skill.list()` gives registered content/path; `SessionContext` has no raw loaded-skill artifact/activation field. | Supervisor-approved artifact readback and runtime selection/activation measurement must join raw digest to loaded content; no reconstructed frontmatter. |
| Stable instance and installed build/child SDK | `src/app.ts:1-5` has name/version/channel; `ServerInfo` has PID/paths/version; neither identifies stable Tessera run instance, installed Tessera build or launched MCP child package. | Trusted supervised process identity plus public installed artifact/package readback; no coding-instance state or credential copying. |
| Included route/entitlement/no-fallback | `src/promise/integration.ts` exposes metadata `connection.active`; `Connection.CredentialInfo` contains ID/label/key-or-OAuth, not billing entitlement, selected transport billing route or fallback enforcement. | Guarded selected provider transport/policy plus independently obtained **public account entitlement** evidence from the approved account/platform. A human permission or source manifest alone is not measured entitlement. Never resolve credentials/inspect auth headers to derive it. |

**Parent next decision:** bind the runtime owner to an approved dedicated guard
producer scope (suggested Tessera path `src/tessera_agent/runtime_admission_guard.py`,
not written here), or authorize a supported dedicated platform exporter for the
missing final-dispatch/effective-state/account fields. Supply the explicit issuer/
HTTPS origin/trust implementation digest/enrollment/instance/principal/public
entitlement/no-fallback policy pins via the secure factory. If platform hooks cannot
measure final dispatch, approve that narrow external API extension rather than
reading undocumented internals or synthesizing measured claims. No API key, paid
spend, OAuth transfer, global/coding config change or inference is necessary to
specify/install this boundary. Live acceptance still requires actual installed
discovery, auth enforcement, measurement receipts and bounded requested workflows.

## Targeted attestation checkpoint

### Final bounded producer continuation

The shipped plugin now implements `produce` through the **same authenticated V2
RPC registration**: `POST /api/rpc/tessera.harness.observer.v1/produce` with the
same location query and `{input:{protocol,nonce,sessionID}}` as `observe`.
Python `V2PluginHarnessObserver.produce_partial()` joins this actual producer output
with separately authenticated control-principal readback. No sideport or coding
instance installation is involved.

Output protocol `tessera-runtime-guard-partial-v1` has `complete:false`, echoed fresh
nonce/session, `issuedAt`/`expiresAt` (30 seconds), `publicObservation`, actual
`agentPermissions`, whitelisted `providerMetadata` (ID/integration ID/package),
`transportObservations`, `sources`, and explicit `missing` fields. There is no
admission/account-included boolean. Output schemas are registered with V2 RPC;
Python rejects a complete flag, mismatched readback, stale times, unknown output
fields and unsafe transport origins. Its returned partial report cannot admit.
The complete attested factory independently rejects this partial protocol.

Producer measurements are from:

- actual transformed `ctx.tool.list()` schemas and `session.context` tool snapshot;
- actual registered loaded `ctx.skill.list()` **content-body** hash (not guessed
  raw-file frontmatter/activation);
- actual `ctx.agent.get()` permission rules and `ctx.provider.get()` ID/integration/
  package fields; provider settings/headers/body are never traversed or copied;
- current session model, runtime app version and selected `connection.active()`
  public ID/method metadata;
- point observations in documented `model.request`, `http.request`, and
  `experimental.ws.handshake` hooks: URL **origin only**, HTTP method where
  available, model IDs/kind/time. No auth header, body, frame or query/path is
  retained. Only explicitly allowed HTTPS/WSS origins pass; userinfo, insecure or
  unknown origins produce no trusted transport observation.

Transport origins are expected human constraints in `allowedTransportOrigins`
plugin options, **not measured authorization/billing proof**. Native hooks may be
followed by later plugins or provider lowering; observations are explicitly
point-in-hook, not last/final dispatch. WebSocket send/receive hooks are deliberately
not registered: their only added information is raw frames, outside this boundary.
Safe supported app/session/model/principal facts are therefore implemented; absent
final/config/identity/account facts are not replaced by options/manifests.

**Fresh official-doc/source reconciliation:**

- Current V2 OpenAPI includes `Session.ProviderContext.Provenance` with provider ID,
  provider/model IDs, route/protocol and an **endpoint digest**. Pinned
  `packages/schema/src/session-provider-context.ts:6-16` explicitly says producing
  model/deployment/route identity, **never credentials or a connection ID**.
  This is useful historical producing-route provenance, not included entitlement.
- In pinned generated public types, `providerContext` occurs on
  `SessionMessageCompactionCompleted` / `SessionCompactionEnded`
  (`packages/client/src/promise/generated/types.ts:1773-1804`), not `Session.Info`
  or the `SessionContext` hook. Reading raw compaction/provider context messages is
  not needed or performed. No unsupported `session.providerContext` API is invented.
- `packages/core/src/session/model-request.ts:261-294` runs model-request hooks,
  then applies their changed base URL/headers and checks producing-route compatibility
  for compaction context. It demonstrates that a point hook observation may later
  differ; endpoint-history compatibility is not a final billing-route attestation.
- The documented plugin API supports `ctx.model.list()` and `ctx.model.default()`;
  `get(providerID,modelID)` exists on the **ModelEditor inside a transform**
  (`packages/plugin/src/promise/model.ts:7-28`), not as `ctx.model.get()` on this
  pinned public client. This plugin performs no no-op transform to obtain a claimed
  final effective config; current selected model is actually read from the session.

**Demonstrated minimal external extension boundary (not edited):** if parent
authorizes a final effective-state/dispatch public exporter, the concrete discovered
OpenCode core surfaces are
`packages/core/src/plugin/hooks.ts` (actual ordered hook registrations) and
`packages/core/src/session/model-request.ts` (post-hook effective request).
Public schema/SDK exposure would additionally require a narrowly typed metadata
schema beside `packages/schema/src/session-provider-context.ts` and an authorized
protocol/SDK route/hook export. This is a request for those specific final metadata
capabilities—not permission to patch them. A trusted Tessera supervisor artifact/
identity exporter and separately measured public entitlement producer remain
needed; neither can be replaced by provenance history. No external source writes,
private internals reads, live coding API calls or OAuth transfers occurred.

The existing complete factory **cannot truthfully succeed using only this shipped
producer**. It can succeed only with an independently authenticated complete guard
whose measurements meet the earlier protocol. There is no fake `active` attestation
emitted by this plugin.

Final continuation checks: **22 passed, 53 deselected (8.34s)** for affected compiled
plugin cases and new partial producer/Python/complete-factory rejection composition.
Fixtures cover drift, later-hook mutation without false finality, unknown OAuth,
unsafe URL/userinfo, nonce/TTL, private-field getter traps and redaction. Tests compile
and execute shipped TypeScript with mock SDK declarations/interfaces; not installed
SDK or live proof. No unchanged full suites or prior guard suite were repeated.

New guard tests: **26 passed, 40 deselected (16.18s)** after correcting the fixture
to use the binding's full closed 18-tool catalog. Following snapshot-age hardening,
the valid production path plus stale/future snapshot negatives: **3 passed,
65 deselected (3.64s)**. These execute the compiled shipped plugin/mock SDK, strict
HTTP adapters, plugin RPC, full real binding verifier and production observer class
with **synthetic authenticated guard fixtures**, not installed/live proof. Prior
compiled/transport/source-suite receipts remain historical and were not repeated.
