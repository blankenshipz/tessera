# Runtime admission v2 — reviewed intent 6, explicit opt-in

## Intent 7 client and co-located integration implementation

### Actual local API / canonical IPC gate (subsequent checkpoint)

`tests/test_runtime_binding_client.py` now includes 31 passing combined cases
across retained selections: actual loopback HTTPS application, temporary SQLite
human/bootstrap and separate subject/publisher credentials, human runtime policy /
enrollment / immutable financial issuance, actual HTTP client resolution/allocation,
Python verification before first claim, canonical `StoreInvocationGate`, real
child-process pipe effect and settlement. All owned TLS servers are stopped; no
active service or installed DB is touched. Test guard/quota/full-install measurements
and child primitive remain explicitly **synthetic upstream inputs**, not private
provider/installed native acceptance. The approved constructor registers the test
producer; requests cannot supply JSON complete/pass flags or select an adapter.

The producer stores independently installed immutable request/wire/session/pins
and an actually time-stamped execution record. Claim references resolve against
that stored record, never a request-derived `NativeCheckWindow` echo. Financial
issuer/assertion comes from real authenticated human issuance; original observed
time is preserved and stale issuance is rejected. No provider entitlement inferred.

Covered negatives include agent issuance, wrong publisher/credential/audience/
project/session, stale original finance age, current subject/operator credential
revocation, parent epoch, immutable-context mutation, financial/policy/enrollment
revocation, exact nonce/ticket/request/fingerprint/order/sequence/check references,
lost actual committed claim reply, readback-only response, concurrent first claims,
restarted canonical one-shot entry, uncertain child, private-check revocation and
wire mutation before native continuation, and contradictory settlement. The
context record is immutable; this gate does **not** claim a context withdrawal API.
Credential/epoch mutations and unavailable replies are labelled test fault injection.

A separate process issues an actual authenticated HTTPS financial revocation while
the Store's canonical native continuation holds its writer fence. The child effect
completes once before that waiting revocation returns 200. This is the explicit
linearization rule, not DB/network atomicity: admission precedes the blocked
revocation; a committed entry cannot refund/retry after uncertainty. Private-resource
changes outside this DB remain subject to the actual native owner's synchronous
live primitive boundary, not an HTTP snapshot.

The actual gate found and repaired a product interoperability mismatch: explicit
runtime namespace action digests now match the server's `{"actions": [...]}`
envelope. Legacy verifier namespace retains its existing sorted-list digest.

Evidence is composite, **not one final 31-pass invocation**: initial actual gate
16 passed (35.17s, exit0); additional selection 15 passed / 2 failed (32.69s, exit1)
includes two affected isolated unit cases and 13 combined cases; repaired two
combined cases 2 passed / 50 deselected (5.56s, exit0). Final scoped lint/whitespace
exit0. Complete failures and successes retained under the private receipt directory;
`COMBINED-API-HANDOFF.md` records exact inventory/revision boundaries. The earlier
unit-only pending-gate statements below are historical and superseded only for this
actual temporary API/canonical-IPC boundary, **not** installed/private native proof.

`src/tessera_mcp/runtime_binding_client.py` now implements actual authenticated
HTTP client operations (resolve, immutable financial reference, allocation, claim,
settlement), enrolled projections, ordered verifier/claim orchestration, an issuer
SQLite invocation lease, and a bounded local process IPC continuation. This is
source implementation, not a claim that real private resource/enrollment bindings
are installed. Existing default v1 and v6 receipt history remain unchanged.

`RuntimeBindingClient` uses only the exact publisher runtime credential reference,
distinct control/worker routes and approved HTTPS origin. It never discovers,
copies, logs or proxies a human credential. Redirects/retries are disabled; malformed,
unknown, duplicate-field, missing or lost replies deny. Only a fresh **201**
first-claim can reach a native continuation; 200 readback is never a permit.

`EnrolledAdmissionAdapter` compares the independently approved enrollment and full
installed pins on every resolution, resolves the original financial reference
freshly, and obtains checks from the approved owner producer. Its check references
must be resolved by the **same registered owner boundary in Store**, not accepted
as JSON passed/complete/verified. Full runtime profile digest is bound by a newly
generated `bind_runtime_profile()` manifest; fresh human enrollment/install pins
are required, never an automatic legacy conversion. Server runtime actions are
explicitly mapped via `authority_action_namespace="runtime"`, while prior verifier
callback namespace remains unchanged. No receipt flag selects either namespace.

`session_observer()` supplies the existing factory's approved v2 observer port with
real allocation/resolution/producer callbacks. It does not auto-start research.
`EnrolledNativeBridge.invoke()` orders actual Python verification before first
claim, native IPC, and immutable settlement. Unknown claim replies start no child;
uncertain native/settlement replies never refund or authorize a retry/fallback.

### Exact issuer hook required for the next API integration gate

The backend's quiescent phase-two handoff now supplies the real canonical hook:
`RuntimeBindingStore.guarded_native_continuation(actor, operation_ref, claim_ref,
ClaimRuntimeOperation, continuation, audience=...)`. `StoreInvocationGate` binds
that hook directly, and `NativeInvocationPipe` executes its ready/invoke exchange
inside the canonical continuation. The Store commits a durable one-shot entry
before reacquiring its writer fence and rechecking live authority/private checks.
Thus a crashed entry cannot be retried merely because the claim remains allocated
or claimed. An authenticated publisher actor and independently registered owner
private live callback must be supplied by the installed issuer boundary, not JSON.
The live callback must enforce by raising on denial, not return admission booleans.
This source wiring has not yet been run against the actual Store/API integration.

The standalone `SQLiteInvocationGate` below is an isolated lease/check exercise
and adapter-construction aid. It is **not** a substitute for the canonical Store's
durable one-shot ledger, nor production admission proof. Parent integration must
use `StoreInvocationGate` (or the native owner's equivalent exact canonical fence).

For an isolated `SQLiteInvocationGate` exercise, its owner registers the exact
issuer connection, with `validate_owned_claim(claim)` returning a fresh
`IssuerInvocationState(claim,snapshot,financial,checks,policy)` inside the already
held `BEGIN IMMEDIATE` transaction. This standalone validator must not start/commit a nested transaction,
return an HTTP snapshot, copy cached eligibility, or accept caller booleans.

That validator must reauthenticate the current publisher/subject/operator and their
credential ancestors; recheck tenant/destination/context/enrollment/policy/runtime
and parent-grant/root revisions/expiry/revocation; validate this exact reserved
claim against its canonical ledger; and read independently registered full pins,
private checks/quota references and immutable financial original observation.
`OwnerCheckExecution` binds local/quota refs, authenticated producer bytes, exact
native expectation and current separately measured installed pins. Missing genuine
producer/issuer registration remains denied, not a successful mock adapter.

The gate itself verifies exact owned reservation, financial scope/age/expiry,
policy, native request/nonce/ticket/order/sequence, model/full pins and current quota.
**Remaining=0 is valid for the last unit's own reservation**, never a global balance
Boolean override. Co-located projections use this held state and do not perform
HTTP reads under the issuer lock. All authority/revocation writers must use the
same DB lock boundary; a foreign/uncoordinated store cannot supply the lease.

Reservation commits before effect. A second bounded writer lease is held across
the private inherited pipe's ready/invoke/native-returned exchange. This defines
explicit revocation ordering: a concurrent DB writer waits until this invocation
lease ends, bounded by the strongest claim/evidence deadline and at most 30 seconds.
Unknown liveness, altered wire reference or interrupted exchange denies/uncertain;
it is not DB+network atomicity or provider exactly-once. The approved native process
must enforce branded immutable wire identity and synchronous private-resource live
checks inside its actual primitive continuation. The pipe cannot certify those
facts from a serialized reply. A remote HTTP snapshot is never that live fence.

The child receives no inherited coding environment/credentials. Only explicit
approved path references (HOME/PYTHONPATH/runtime-resource/publisher-credential file)
are supported; no token arguments, raw wire/body/header/account/prompt frames are
provided by this adapter. Metadata frames are bounded and exact; native errors are
sanitized, no silent fallback or replacement process.

### Current bounded verification / honest pending gate

Client/unit tests deliberately use **SYNTHETIC HTTP responses and issuer/private
facts**, actual Python verifier/SQLite locks and actual local child-process IPC.
They do not import or test the backend/API owner's changing files. The first
unit gate retained one failure/15 passes and lint failures; the expanded gate
retained two failures/19 passes. Fixture time/canonical UTC rendering repairs
preserve original observation instants, not freshness upgrades. The two affected
projection/orchestration tests then passed, with 19 unaffected cases deselected.
Private full logs/inventories/exits are retained locally under evidence ID
`RUNTIME-BINDING-INTEGRATION-V7`; they are not distributed with this document.

The real authenticated human-issuance → registered publisher/financial resolver →
Python verification → native claim/continuation API integration gate waits for the
parent's backend/native quiescent handoff and actual issuer hook registration.
These unit passes are not that gate, real account proof or installed SDK linkage.
Prior 106/123/22 and v5 RC receipts are retained and not rerun/relabelled.

Semantic correction: assurance is `local-enforcement-not-provider-billing`, **not**
provider entitlement or billing certification. V1 remains the default and strict.
No v2 receipt becomes `included_oauth_verified=True`, legacy entitlement pins, or a
diagnostic `complete=True`. No automatic conversion/mixed version/fallback exists.

## Minimal native runtime-owner protocol handoff

`src/tessera_mcp/runtime_admission.py` defines the closed wire `Evidence` model;
use `Evidence.model_json_schema()` for its exact schema. Version and assurance are
literal `tessera-runtime-admission-v2` / `local-enforcement-not-provider-billing`.

- Independent expected `AdmissionPolicy`: policy reference/revision, **required**
  financial maximum age seconds, evidence maximum age <=30s, approved operator
  issuer, explicit approval of unknown provider entitlement/billing. Digest is
  SHA256 canonical sorted compact JSON. It is not a provider entitlement digest.
- Independent `Scope`: issuer/publisher SHA256 reference/implementation/enrollment/principal/credential,
  opaque owner SHA256 reference (never raw account ID), project/destination,
  session SHA256 reference, role/context reference/body digest, exact effective
  actions digest, grant revision/epoch, policy ref/digest. Independently approved
  and current, never adopted from signed caller claims.
- `Pins`: instance, SHA256 build/SDK/harness_schema/effective config/loaded skill/full typed
  catalog/full tool mapping/binding/mandatory terminal implementation. The installed
  measurement port must independently return exact matching full pins.
- `Evidence`: version/assurance, unique SHA256 nonce and ticket, phase,
  observed_at/valid_until, scope/pins, completed-native local checks, quota times,
  operator financial attestation and unknown provider facts, native order revision/
  sequence/request reference/final fingerprint. **No `complete` field**.
- Phases: `session-preparation` returns `session-ready-no-dispatch` only with
  installed mandatory terminal capability; `terminal-http`/`terminal-ws` require
  current native `TerminalExpectation` after mutation/lowering at last pre-effect
   boundary. Request reference/fingerprint/order/sequence and independently issued
   nonce/ticket must match exactly.
- `NativeTerminalPort.installed_pins()` is independently approved measurement;
  `bind_verifier(verify_terminal)` installs mandatory synchronous pre-effect checks
  and returns a tick adapter `(supervisor, run_id, prompt)`. It must not run an
  unguarded supervisor first, authorize `continuePrepared`, or join AFTER dispatch.
  `verify_terminal(raw_bytes, TerminalExpectation)` validates dispatch before effect;
  its result names the exact ticket/request/fingerprint/order/sequence. Native owner
  must enforce one-shot use, final live rechecks and adjacency to transport effect.

Python verifier success is not evidence that a binding actually installed this
native enforcement. The embedding and native port must be separately approved;
absent ports/default production fail closed. No Basic username, environment identity,
JSON boolean or signature alone supplies enrolled principal or truthful execution.
Current enrollment/grant/context/budget checks are mandatory trusted callback ports,
in addition to all existing supervisor/tool/context/disclosure fences.

`current_local_checks(operation, expected)` is a mandatory independent native port
returning `CurrentLocalState` (typed completed local checks, current quota, actual
execution time, native expectation). It cannot echo receipt JSON or infer execution
from a signature. The verifier rereads it alongside operator status and authority
before release and denies changed/future/stale state. A cached session admission
never replaces these reads. Native ordering/final effect adjacency still belong to
the approved installed owner adapter; serialization alone proves neither.

Financial evidence requires separately authenticated **operator** issuer, exact
owner/project/destination/policy revision, original observed_at, asserted_at,
expiry and current revocation. Zero purchased balance, top-up/reload off remain
required. Date-only old evidence cannot invent a time; max age is a required policy
input, never a permissive default. Quota snapshot <= checked <= execution <= now,
expiry bounded to snapshot+30s. Any missing/unknown required LOCAL fact denies.
Provider facts are only `unknown` under explicitly approved v2 policy.

Private SQLite nonce/ticket/native-request-scope ledger commits pending before validation, then validated
or uncertain. Replay/uncertain/crash survives restart and cannot retry. It is not a
generic attestation service or an independent truth/signature proof.

## Selection and proof limits

### Native Effect handshake (read-only runtime-owner source handoff)

`packages/ai/src/route/terminal-boundary.ts` supplies the branded
`makeTerminalRuntime(binding, principalCapability, ports, approvedNativeFetch, clock)`.
This Python lane does not manufacture that brand or call `continuePrepared`.
`TERMINAL-PROTOCOL.md` distinguishes the branded capability from
`opencode-terminal-dispatch-v2` **diagnostic records**; those records are not accepted
as `Evidence` and cannot reconstruct native authority.

The approved embedding must bind `NativeTerminalPort` to this actual Effect runtime:

| Native binding / port | Python expectation / obligation |
| --- | --- |
| publisher_ref; principal/instance/session/project/destination/model/grant/enrollment/root refs; request_nonce | Independently approved native opaque-reference mapping to `Scope`, `Pins` and per-operation `TerminalExpectation`; no identity derived from JSON or invented issuer/root/enrollment mapping |
| policy/build/config/skill/catalog SHA256; order/grant/enrollment revisions; epoch-ms expiry | Compare exact current native snapshot against separately approved context; Python full SDK/schema/tool-mapping pins remain additionally mandatory, not replaced by native compact pins |
| currentAuthority, opaque principal capability | Preserve actual object identity in native owner, reauthorize every operation; never export/rehydrate principal capability through Python records |
| assertNativeOAuthAccountRouteNoFallback, assertFinalModelToolsAndOverrides, checkNativeQuota | Independently provide current completed local checks and native final-wire fingerprint/quota; no successful missing defaults, caller receipts or old exported diagnostics |
| assertOperatorPolicyV2({current,wire,operation_ref,quota}) | Run the actual v2 verifier against that immutable final-wire/current scope before claim/effect; resolve the genuine immutable financial assertion/current issuer and owner mapping. Return an invocation-bound `assertLiveForInvocation(wire,operation_ref)` release; no research grant/context/approval repurposing |
| nextOperation, claimCurrent | Native issuer owns durable operation identity and **atomic** current authority/revision/expiry/revocation/budget claim. Full Python `verify_terminal` must run against exact immutable final-wire state BEFORE this claim/native effect, not merely during preparation or after native return. Approval/result must stay bound to the same operation and native lock/scope; Python validation alone is not a budget debit or transport permit |
| settle | Persist returned/uncertain; returned is not provider acceptance. Failed/uncertain operations cannot be automatically reminted for retry/fallback/restart |

Native phases HTTP-fetch/WS-handshake/WS-frame each need the exact corresponding
fresh terminal expectation and own one-shot operation; a reused socket does not
reuse prior admission. No timestamp/reference/sequence is fabricated to adapt a
record. Custom non-Fetch transport, redirect or arbitrary socket are unsupported,
not assumed covered. Native full final-wire checks/brand/claim adjacency remain
runtime-owner work; the missing genuine embedding makes production deny.

The inspected handoff defines live Effect ports but does not supply a genuine
financial resolver, enrollment mapping or installed Python bridge. A test-only
process callback now demonstrates actual Python verification before the native
primitive with synthetic resources; it is not the missing installed adapter.
`NativeTerminalPort` is the explicit fail-closed interface. Tests reject success
flags/diagnostic records as tick adapters and unsupported transports.

Role config explicitly selects `admission_version="v2"` and policy ref/digest,
without legacy guard entitlement configuration. Owner constructs approved
`EvidenceBackedHarnessObserver` and `RuntimeAdmissionVerifier`; factory accepts its
callback only through explicit `admission_observer`. Consumer persists v2 selection
and policy digest and binds mandatory native tick before any reasoning effect.
V1 config/default guard and legacy verifier remain unchanged. Diagnostics stay
diagnostics. No live credentials, principal enrollment or actual installed terminal
bridge are supplied by source tests. No inferred transport finality from post-hook
stage evidence. Historical v5 candidate/gates remain frozen and distinct; source
changes require a new parent-owned inventory/candidate/gate matrix.

## Local evidence (distinct selections, not additive totals)

All private stdout/stderr, command, native exit and source inventories remain in
local evidence records indexed as `RUNTIME-ADMISSION-V6`. Failed runs are retained,
not overwritten; these record IDs are not links to public artifacts.

| Selection | Observed result / private receipt directory |
| --- | --- |
| Historical initial v2 plus v1 regression gate | 246 passed / 1 legacy partial-config failure, exit1; `focused-bvrdoj0p` |
| Historical compatibility repair and expanded cases | 121 passed, 3 dependency warnings, 53.65s, exit0; separate lint failure retained; `repair-ot0_ly6j` |
| Final affected verifier/factory/service integration | 123 passed, 3 dependency warnings, 10.88s, exit0; `final-qesq2zhn` |
| Final verifier-only selection | 106 passed, 0.84s, exit0; `native-handoff-zrrv2rrd` |
| Actual native/Python interop, after test-adapter release fix | 20 passed / 2 HTTP failures, exit1; all 8 WS cases passed with individual Bun exits0; `interop-repair-fum1ct0w` |
| Affected HTTP interop repair only | 14 passed, 8 deselected, zero skips, 6.19s, exit0; `interop-http-repair-o0q8wte4` |

All **22 interop cases have passing coverage**: latest 14 HTTP plus 8 unaffected WS
from the preceding run. This is not one 22-pass/exit0 invocation. The HTTP-only
fixture repair made content type explicit; unchanged WS checks were not repeated.
The earlier all-22 failed adapter run (`interop-live-code-2k4k9lmj`) also remains.

Native source revision is
`59d771e9ba6dc1db15af4915932ccc99f84dc7dab938aaa54f3fb4718d438f69`,
not the Python verifier file hash
`cb81a5878fad77955a59b56673e52400b17af6b2c9e998e414d368606265de94`.
Runtime-owner final receipts separately report 58 native and 10 preparation cases,
each exit0, plus 33 original / 45 reconstructed target files; interop verified all
native inventory pins before/after execution. Counts across selections are not summed.
Historical v5 RC/4503 evidence is immutable, not relabelled as v6 acceptance.

Fixtures explicitly name SYNTHETIC authority/credential/issuer/native-check ports.
They execute the actual local closed verifier, SQLite, observer, binding, factory
selection and consumer fences. Rejected paths have zero native/provider effects;
no live inference, real owner account or installed terminal adapter was exercised.
Existing v1 binding/observer/consumer tests passed in the first run; the affected v1
factory regression passed after repair. No full 4503/4268 gate rerun.

Actual native Effect transports, branded terminal code and actual Python verifier
execute in the interop harness before fake approved fetch/WS constructor/send.
Credentials, enrolled authority, operator assertion, private guard and quota inputs
are explicitly **SYNTHETIC**. Positive target primitives execute once; denied
targets execute zero times (WS frame negatives have a successful setup handshake).
Real provider/network effects are zero. Native claim fixtures use in-memory state/
counters; Python's actual SQLite replay ledger is durable across process restart.
Neither proves a production atomic remote grant/budget transaction or native claim
durability. Installed SDK/build linkage, full pins, final transport primitive trust,
genuine owner/principal enrollment and current financial/private guard/native quota
adapters remain unprovided. The test-only process callback is not installed proof.

Operator maximum observation age is the required policy input
`max_financial_age_seconds` (operator max-age semantics), with explicit scope,
original observed/asserted timestamps, expiry and current revocation. There is no
permissive default, invented date-only timestamp or measured legacy upgrade.
Only provider entitlement/billing may remain unknown under explicit human-approved
v2 policy; **unknown required local enforcement still denies**. State-file
`complete`/`verified`/`authorized` booleans cannot create authority or a release.

The [actual operator authority map](operator-financial-attestation.md) identifies
reusable server-derived identity/project/current-lineage checks but no financial
issuance/reference/current-revocation/profile-age/runtime-owner mapping adapter.
Plan approvals explicitly grant no execution; approved grants, capability policies
and curated context JSON are not financial assertions. Their creation timestamps
cannot become financial observation times. Absent genuine financial resolution
ports admission denies; the approved semantic choice is not a generic approval ask.
