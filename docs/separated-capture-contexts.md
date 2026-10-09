# Separate curated input and public capture policy (revision 3)

The generated-output registry's `pins.context_id` is the **curated input**, not
public-capture policy JSON. `pins.context_digest` is SHA256 of the actual input
body's UTF-8 bytes, not the context envelope digest or a self-referential policy
hash. Both input and policy are independently human-created immutable ordinary
`agent-contexts` records scoped to the same tenant/project/credential/destination.
No backend schema/API/registry change is needed. Policy cannot issue a grant,
approve an experiment or confer output disclosure by itself.

## Factory-facing fields and creation order

The existing `CaptureBinding` and `PublicObservation` gain an optional, all-or-none
triple (absent fields remain omitted from legacy serialization/ACK/request digests):

- `input_context_id`: actual curated input context ID; must equal existing `context_id`.
- `input_body_digest`: 64 lowercase SHA256 hex; must equal existing `context_digest`.
- `capture_policy_context_id`: independently human-issued policy context ID, distinct
  from input. It is pinned externally, never a self-referential field in policy JSON.

1. Human creates the ordinary approved input context with the actual curated body.
2. Read that actual body through the authenticated backend and calculate its UTF-8
   SHA256. Do not echo a digest supplied by a caller/runtime admission boolean.
3. Human separately creates a policy context whose ordinary `body` is strict
   `SeparatedPublicHarnessDisclosure` JSON:
   `schema_version:tessera-separated-public-harness-disclosure-v1`,
   `input_context_id`, `input_body_digest`, `context_digest` (same input hash),
   `model_id`, `session_id`, `harness_instance_id`, `run_id`, nullable `task_id`,
   `skill_digest`, `config_digest`, `allowed_public_events`.
   Policy body does not contain its own context ID or digest. Use the returned
   policy context ID in binding, not as the registry/input context.
4. Human generated-output admission/registration keeps the existing backend
   `OutputPins` contract unchanged: context ID/body hash refer to the input; policy
   references are capture metadata, not backend-generated-output authority.

Both contexts must have actual human creators, live issuer/reader identity,
explicit disclosure approval and public/project classification. An artifact context
has a separate custody/readback contract and is rejected by this ordinary-body
resolver rather than silently substituted. Context creation itself rejects
`disclosure_approved:false`; output recipient disclosure still has its independent
revocation/expiry fences. Copying a supported policy JSON body into another context
does not turn it into curated input. Legacy one-context records remain historical
valid metadata; new ordinary runtime input readback must use the separate contract.

## Exact trusted ports

`public_capture_contexts.resolve_separated_capture_contexts(connection, principal,
binding)` is the concrete **transaction-local** resolver for authenticated backend
integration. It requires a caller-owned transaction and existing authenticated
agent principal reference, not a token read from environment or owner proxy. It
rehydrates live identity, checks context:read and provenance:append enrollment,
tenant/project/destination, and reads both actual immutable rows and human issuers.
No body is derived from supplied pins. Hash verification happens against the actual
input body before any capture envelope can be accepted.

Return contract (`SeparatedCaptureResolution`):

```
{
  input_context: ResolvedCaptureContext,
  capture_policy: ResolvedCaptureContext
}
```

Each `ResolvedCaptureContext` contains actual `context_id`, `org_id`, `project_id`,
`credential_id`, `destination_digest`, `issuer_principal_id`, `issuer_role`,
`disclosure_approved`, `classification`, and `body` (ordinary backend max 4000).
These are actual readback fields, not an admission manifest echoed back as observed
state. `validate_separated_resolution(binding,readback)` applies the same closed
contract to a trusted authenticated HTTP reader's normalized response. Runtime owner
must provide that reader for its actual deployment/transport; a callback fabricating
human-role or readback values is not an implementation of the port.

`PublicCaptureAdapter(binding,disclosure_resolver,output_resolver)` uses the structured
resolution when the triple is present; legacy resolver shape is unchanged when it
is absent. `.resolve_contexts()` returns validated **ephemeral** input/policy readback.
Factory input reader must supply only `resolution.input_context.body` to the curated
input channel and compare its digest to the approved backend input pins. Policy body
is interpreted only as capture policy; it must not be handed to the model as input.
Do not journal/log the resolution bodies. `.envelope(observation)` persists only
strict redacted metadata, including the separate reference triple, never either body.
Actual registered research-output family validation/disclosure remains mandatory.

`NormalizedPublicMetadataProducer(adapter,read_approved_metadata,batch_limit=100)`
implements the runtime's `reader.read_public_metadata(row)` **normalized port**.
It requires the separate context contract, checks row `id`/`session_id`, resolves
actual input/policy, and validates each already-approved `PublicObservation` through
the adapter before returning a list. Batch is bounded to 1..100. Upstream stable
event IDs, sequences, watermarks and declared eligible-public gaps are retained;
no private parts are counted and no IDs/gaps are invented. Raw V2 envelopes,
prompts/tool bodies/private reasoning, extra fields and overflow fail closed with
content-free errors before persistence. This is not a V2 extractor or planner loop.

Collector command **and replay** re-read the actual contexts transaction-locally,
check policy pins/public categories, body integrity, generated registry correspondence
and current output admission/disclosure. Active run cannot change input/policy IDs
or body digest by rekeying an event. Outbox preserves prior key/body uncertainty;
legacy absent fields do not change historical serialized binding/envelope shape.

## Remaining runtime-owned binding

Source factory can consume the triple, `adapter.resolve_contexts()` input channel,
and `NormalizedPublicMetadataProducer.read_public_metadata(row)` capture channel.
It still needs actual authenticated context/output readers, installed harness input
readback, and a real eligible-public V2 extraction implementation with independent
visibility/admission observations. This lane does not edit runtime/factory/agent,
backend/migration, peer/MCP or environment files. No denied environment file was
read or replaced through another path. No host/auth discovery, inference or live
deployment occurred. Tests use actual temporary backend contexts, registration,
collector and SQLite plus synthetic research text/model/public observations; they
do not establish a live generated planner/critic or installed factory journey.
