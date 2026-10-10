# Runtime binding/control contract — version1

Status: closed DTOs with authenticated Store/API implementation.
`src/tessera_control/runtime_binding_models.py` defines closed immutable wire models.
Routes, Store methods and additive migrations are implemented. **The actual installed
owner-private/native adapter remains an integration prerequisite, not a fake default.**
Legacy defaults/opt-in admissionv2 remain unchanged.
DTO validation and an authenticated response are not authorization or evidence that
a native effect occurred. No provider identity, OAuth credential or raw request data
may enter these public records. Upstream entitlement/billing remain `unknown`.

## Identity and issuance

Existing `api.principal`/`worker_principal` call Store authentication with distinct
control/worker audiences. Store rechecks credential ancestors, organization, expiry
and revocation; future commands also call `_live_principal` inside the transaction.
External scopes require explicit handling, not generic owner APIs. Use the existing
AgentAuthorityStore human eligibility rule, never caller-supplied `role`, issuer,
actor ID, created_by or authorization flags. Request models deliberately omit them.

Only a current eligible human control credential issues/revokes policy, enrollment
and financial records. An agent cannot self-enroll or attest financial settings.
Enrollment names an independently human-approved publisher credential, distinct from
the model-agent credential. It must already exist, have the specified audience and
current subject/project lineage. No enrollment endpoint mints or copies an OAuth
credential, promotes an agent, or returns a secret. A worker token never uses control
routes; the control and worker publisher routes are distinct adapters to the same
Store transaction. Publisher credentials cannot be local_owner/human_session tokens
used as proxies. An existing agent-kind control credential may be a publisher only
through explicit separate human enrollment; ordinary agent membership is insufficient.

Policy issuance is immutable and explicit `tessera-runtime-admission-v2` /
`local-enforcement-not-provider-billing`, with providerunknown explicitly approved,
age bounds, allowed native phases/actions/model, count and fixed per-call maximum
exposure. Human enrollment issues a dedicated bounded runtime grant, linked to the
existing parent agent authority revision/epoch/subject/model/destination/context.
This is NEW human runtime authority, not conversion of an attempt grant into model
inference authority. Do not extend existing AuthorityAction literals implicitly.
Allowed runtime actions are exactly `runtime:prepare` and `runtime:invoke`; original
experiment/delegation grants retain their own actions and budget accounting.
All parents must remain live; child expiry/actions/exposure/count cannot widen any
applicable parent constraint. Any needed additional parent permission needs human
issuance, not a request Boolean. One policy cannot authorize arbitrary other models.

RuntimeScope fields match Pythonv2 Scope exactly; RuntimePins fields match Pins.
RuntimePolicyRecord.operator_issuer is derived from its authenticated human issuer;
financial issuance must use that same current eligible operator (no caller replacement).
The v2 AdmissionPolicy projection is version,assurance,admission_policy_ref=policy_ref,
revision,max_financial_age_seconds,max_evidence_age_seconds,provider_unknown,
operator_issuer. Its canonical digest is admission_policy_digest and Scope.policy_digest.
The full profile_digest additionally binds action/phase/model/count/exposure bounds;
enrollment's independently measured Pins.binding must bind that profile digest too.
Scope.grant is the dedicated server-issued runtime grant, not the parent experiment
grant ID. Server derives issuer/principal/credential/project/policy/actions digests;
context human approval and digest are read transactionally. Human supplies opaque
publisher/owner/session and installation pin references, but the owner enrollment
adapter independently measures and matches them before enrollment. Store never treats
user-selected hashes as proof of current private owner/account or installed build.
Current root/session/principal/credential/publisher/implementation/policy/grant pins
must match both the enrolled root and actual owner/native adapter. A MAC/signature
authenticates that publisher/channel, not truth of measured facts.

## Financial assertions

`IssueRuntimeFinancialAssertion` is human-only. Server derives issuer, opaque owner,
project, destination and policy revision from the current enrollment/policy; server
sets asserted_at once. Original observed_at must not be future and cannot be reset on
read/replay/reissue; new issuance is a new immutable reference, never renewal of old
observation. Require observed_at <= asserted_at < valid_until, current time within
both the policy maximum financial age and expiry, issuer/enrollment/policy/grant live,
and exact project/destination/owner binding. Expiry cannot outlive any binding.
Purchased balance is exact integer0; top-up/reload are exact Booleantrue for off.
This is scoped operator attestation, not measured provider/account guarantee. Requests
cannot specify complete/verified/provider entitlement/account IDs or secret headers.
Current issuer eligibility and append-only revocation must be checked on EVERY resolve,
allocation, claim and invocation bridge validation. Replaying old issuance does not
extend validity. Resolver emits `RuntimeFinancialRecord`, matching v2 Financial names;
`RuntimeBindingSnapshot` carries refs/freshness, not a fabricated guard verdict.

## Implemented endpoints and Store interface (phase two)

Every POST uses existing bounded Idempotency-Key. The route project ID and database
tenant constrain access. Key scope is actor credential+method+route, digest exact
canonical request; different content conflicts; replay rechecks CURRENT authority.

| Route | Body -> response | Auth | RuntimeBindingStore method |
|---|---|---|---|
| POST /projects/{project_id}/runtime-policies | IssueRuntimePolicy -> RuntimePolicyRecord,201 | human control | issue_policy(actor,project,key,payload) |
| POST /runtime-policies/{policy_ref}/revoke | RevokeRuntimeRecord -> record ref/revision,200 | human control | revoke_policy(actor,ref,key,payload) |
| POST /projects/{project_id}/runtime-enrollments | IssueRuntimeEnrollment -> RuntimeEnrollmentRecord,201 | human control | issue_enrollment(actor,project,key,payload) |
| POST /runtime-enrollments/{enrollment_ref}/revoke | RevokeRuntimeRecord -> ref/revision,200 | human control | revoke_enrollment(actor,ref,key,payload) |
| POST /projects/{project_id}/runtime-financial-assertions | IssueRuntimeFinancialAssertion -> RuntimeFinancialRecord,201 | human control | issue_financial(actor,project,key,payload) |
| POST /runtime-financial-assertions/{financial_ref}/revoke | RevokeRuntimeRecord -> ref/revision,200 | human control | revoke_financial(actor,ref,key,payload) |
| GET /runtime-financial-assertions/{financial_ref} | RuntimeFinancialRecord | enrolled publisher/current scoped subject read | resolve_financial(actor,ref) |
| POST /runtime-bindings/resolve | ResolveRuntimeBinding -> RuntimeBindingSnapshot,200 | enrolled control publisher or scoped subject read | resolve_binding(actor,key,payload,audience='control') |
| POST /runtime-operations | AllocateRuntimeOperation -> RuntimeOperation,201 | enrolled control publisher only | allocate_operation(actor,key,payload,audience='control') |
| POST /runtime-operations/{operation_ref}/claim | ClaimRuntimeOperation -> RuntimeClaimReceipt,201fresh/200readback | same enrolled control publisher | claim_operation(actor,ref,key,payload,audience='control') |
| POST /runtime-operations/{operation_ref}/settle | SettleRuntimeOperation -> RuntimeOperation,200 | same enrolled control publisher | settle_operation(actor,ref,key,payload,audience='control') |
| GET /runtime-operations/{operation_ref} | RuntimeOperation,read-only | same publisher/current scoped subject | get_operation(actor,ref,audience='control') |

Worker-audience publisher counterparts for resolve/allocation/claim/settle/read are
the SAME relative paths prefixed `/worker` and use worker_principal. Worker access to
financial refs is `/worker/runtime-financial-assertions/{financial_ref}`. No issuer
routes under /worker. Subject reads cannot allocate/claim/settle. Reads of consumed
operation audit under revoked grants are separately read-only and never revive expiry
or grant authority; expired credential still401. Phase2 must explicitly implement
that read-only policy, not accidentally authorize commands with it.

Safe errors use RuntimeBindingError;401 credential invalid,403 actor_not_eligible,
revoked/expired/binding_mismatch/budget_exhausted/stale_*;404 tenant-fenced not_found;
409 revision_conflict/idempotency_conflict/operation_replayed/operation_uncertain/
claim_mismatch;503 native_bridge_unavailable. Opaque error codes only, no account,
headers, prompts, raw native error or private assertion body. Unknown error never pass.

## Native allocation, claim and settlement linearization

Server Store owns the canonical claim ledger; native process is an authenticated
adapter/client, not another independent budget authority or client JSON ledger.
Native crash journal may preserve operation/claim refs and conservative uncertainty,
but cannot allocate new authority, clear server claims, or decide safe retries.
Allocation creates server-generated operation_ref/root_ref/nonce/ticket under a
trusted publisher's independently approved logical request. Idempotent replay returns
the original identity; model-random retry IDs/root refs are never accepted. Unique
root scope binds enrolled runtime session/currentgrant/logical request and native
effect sequence. Store prevents new allocation disguising a retry of a claimed/
uncertain logical root; uncertain previous session root fences automatic subsequent
effects until human reconciliation. Separate WS handshake/frame each requires its own
issued operation, nonce/ticket/sequence. No preparation token authorizes terminal send.

Preparation allocation is readiness-only and cannot be claimed for an external effect.
For terminal HTTP/WS, the trusted owner adapter obtains actual immutable lowered wire,
current guard/quota execution and verifier-issued capability FIRST. Request/fingerprint
digests represent approved safe structural projection/opaque wire ref, never a digest
of raw prompt/body/auth/account. Local_check_ref/quota_check_ref name independently
approved owner-private check executions bound to exact nonce/operation/pins/freshness;
server obtains them through the approved adapter, not arbitrary agent JSON. No HTTP
endpoint here accepts guard=true or successful native defaults. Missing actual adapter
means503/failclosed; these DTOs do not fabricate it. Native verifier is called before
claim; claim alone cannot substitute for mandatory installed v2 verification.

In ONE BEGIN IMMEDIATE state-event-idempotency transaction: reauthenticate actor and
publisher mapping; recheck subject/issuer/credential/grant/delegation/policy/financial/
context/enrollment expiry/revocation/revisions and root; independently resolve trusted
checks/quota refs and mandatory installed pins; compare current nonce/ticket/fingerprint/
request/order/sequence; atomically transition allocated->claimed, reserve exactly one
native operation and fixed policy exposure against runtime and applicable parent budget,
write immutable claim/event/idempotency response. No request-selected charge or refund.
Root/operation/nonce uniqueness rejects concurrent second effect even with another key.
Reserve until the strongest applicable expiry and evidence deadline; expired reservation
never silently resets/refunds its units. Request nonce is not the client's idempotency key.

The last available unit is validly reserved with remaining=0. Invocation resolver must
test **this claim's own valid reservation**, not require globalremaining>0 again.
It must still reject changed epoch/revision/expiry/revocation/finance/pins/quota/wire.
Do not flip budget_available based on caller flags: trusted CurrentAuthority resolver
may report availability for this owned reservation only after ledger validation.
ClaimReceipt binds actorcredential/Scope/Pins/grant epoch/revision,nonce/ticket/exact
expectation,financial/check refs,expiry,claimrevision and atomic remaining budget.
First201 reply says first-claim; any replay is200 readback-only, never a new permit.

There is NO atomic DB+network transaction and NO exactly-once provider claim. Commit
reservation before native effect; lost claim reply/restart/claimed-without-settlement
is ambiguous and denies automatic retry/fallback/refund/reuse. Trusted currentAuthority
and VerifiedTerminalCapability must synchronously assert live INSIDE the exact native
continuation after claim, with actual immutable wire identity/hook-order checks. HTTP
readback or serialized receipt cannot implement that synchronous revocation fence.
The actual bridge must define a reviewed co-located synchronization/lease boundary;
unknown cross-process liveness denies, not an optimistic lastHTTP snapshot. Offline
revocation limits must be explicit. Backendtransaction only proves reservation time.

Native settle appends immutable claimed->native-returned or claimed->uncertain once,
same publisher/claim/expectedrevision; readback replay allowed without another effect.
Contradictory later settlement conflicts. native-returned means primitive returned,
not provider acceptance/billing/experiment success. Connection interruption/lost reply
is uncertain. Neither result automatically refunds or authorizes retries; new root
effects require current separate issuance/disposition review. Reconciliation with
positive safe no-effect proof is a future explicitly human-scoped command, not in v1.

## Storage schema

Immutable JSON records hold full scope/pins/bounds while dedicated columns enforce
identity/uniqueness. `runtime_bindings.py` owns tables and `RuntimeBindingStore`;
`runtime_binding_api.py` owns the router. Migrations and tenant-scoped Store helpers
remain authoritative. Coverage is in `tests/test_runtime_bindings.py` and
`tests/test_runtime_binding_api.py`.

Tables: runtime_policy_profiles(ref,org,project,humanissuer,immutablebody,digest);
runtime_enrollments(ref,org,project,issuer,subjectcredential,publishercredential,
audience,policyref,parentgrant,scope/pins,expiry); runtime_authority_grants(ref,
enrollment,parentlineage,revision,epoch,actions,maxcount/exposure,reservedcount/cents);
runtime_financial_assertions(ref,enrollment,issuer,originalobserved/asserted/expiry,
policyrevision,immutablefinancialbody); runtime_binding_revocations(ref,kind,target,
expectedrevision,issuer,time); runtime_native_operations(opref,org,root,enrollment,
publisher,nonce,ticket,expectation,state,expiry,revision); runtime_native_claims(ref,
opref,epoch/revision,pins/checkrefs,reservation,expiry); runtime_native_settlements(ref,
claim,disposition,actor,time); runtime_native_root_fences(root,state,sessionbinding).
Optional trustedcheck-reference adapter persistence must not store private snapshots.
Use existing events/idempotency tables in the SAME transaction. Tenant FKs/unique
constraints; append-only assertions/claims/settlements/revocations; mutable counters
and projectedstate only under CAS. No storing MAC keys/OAuth tokens in these tables.

Phase2 tests: human/agent/worker separation; tenant/audience/delegation/issuer current
checks; financial originalage/revocation/replay; policy opt-in/defaultlegacy; missing
nativecheckadapter; exact pins and late mutations; concurrent claim/idempotent readback;
restart/lostreply/uncertain root budget no-refund; lastunitreservation; settlement
conflicts; revocation DURING claim/nativehandoff denies effect via actual trusted port.
HTTP integration owner must use real API/resolvers, not mocks as production ports.
Phase-one evidence remains DTO/schema-only. Phase-two tests exercise real temporary
credential authentication, SQLite and FastAPI, with synthetic private check facts.
The exact implementation, callbacks, co-located writer fence and remaining installed
bridge obligations are indexed as local evidence ID `RUNTIME-BINDINGS-V7-PHASE2`.
The additional append-only `runtime_native_entries` table records consumed one-shot
native bridge entry before the effect. No HTTP readback can invoke that bridge.
Historical RC gates remain historical; new source changes require new convergence gates.
