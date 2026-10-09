# Registered public output transport (intent revision 3)

## Research-generated family (schema 53 continuation)

`generated.family:"research-generated-v1"` is a separately discriminated closed
wire family. It requires `output_id`, `disclosure_id`, actual `actor_id`,
`critic_credential_id`, `org_id`, `project_id`, `model_id`, `run_id`, `session_id`,
`harness_instance_id`, `context_id`, `context_digest`, `skill_digest`, `config_digest`,
central `task_id`, nullable `operation_id`, `source_event_id`, `source_sequence`,
`content_digest`, `registration_grant_id`, `registration_revision` and
`output_kind:"critic_review"`. `reference` equals `output_id`. A transport-bound
central task is required; an absent operation remains null, never fabricated.
Mixing worker manifest/collector fields into this family or extra content/public
flags fails closed. Existing worker-family envelopes below remain compatible.

The receiving peer uses **its own enrolled credential** to GET only
`/generated-outputs/{output_id}/metadata?disclosure_id={disclosure_id}`. The existing
backend route validates current human recipient disclosure, credential, destination,
tenant/project and stored hash/integrity before returning bounded metadata without
bytes. The adapter compares exact output/artifact ID, actual writer/credential/
org/project, model/run/session/task/context/skill/config/source pins, hash/digest,
registration grant/revision, public critic kind, human-admitted custody,
self-reported attribution, not-assessed scientific validity and no established
execution. Returned `content_base64` fails closed. No caller's admission/public
boolean is proof. Size is at most 64 KiB, response at most 256 KiB, timeout five
seconds, redirects disabled, and no arbitrary source URLs/paths are accepted.

This family neither requires nor synthesizes a worker manifest, Job identity,
custody release, execution grant or collector sequence. The generated-output
registration grant is a distinct bounded **registration** right; its later
revocation blocks new registration/replay, not a currently disclosed immutable
historical output. The separate narrowed A2A transport grant and current recipient
disclosure are still rechecked for each transition and explicit planner ACK.

Transport history/private outbox links freeze all research registration pins.
The optional **paired** `capture_record_id`/`capture_sequence` adds authoritative
typed public-capture linkage. Neither alone is accepted. Before each transition,
the receiving peer reads that exact collector sequence under its own credential
and verifies `record.generated_output`'s `generated_public_research_output_v1`
family, output/digest/kind and actual writer actor/credential/org, together with
record model, public harness run/session/context/skill/config/source pins.
Missing, mismatched or disclosure-withheld capture fails closed. The original
23-field registration-only form remains compatible and partial; it is not relabelled
as captured public metadata. Linked capture pins are frozen through task restart.

The typed generated-output **author** must append its own capture using its own
credential and separately human-issued author output disclosure. The receiving
peer must not insert `record.generated_output` as if it authored the critic output:
the collector intentionally rejects that identity forgery. Instead every receiver
transport outbox record links the verified author record through the existing
`references.provenance_record_id`, preserving receiving-peer attribution. Private
`links` still strip at drain. Notebook shows the distinct author generated record,
registration event and receiver transport/ACK records in one authorized snapshot.
The historical author disclosure in the record is not the planner's permission;
projection reports the planner's own current `output.recipient_disclosure_id`.
Revocation withholds output/digest metadata and blocks even ACK replay. No worker
manifest, operation, output proof, author credential or captured event is fabricated.

The new focused integration uses real isolated backend HTTP/temporary SQLite
human admission/registration/disclosure and official SDK routes. Output text/model
source is synthetic; SDK server routes use local TestClient, not independently
running peers. It checks exact metadata lookup, all research pins, explicit ACK,
restart/replay, transport/disclosure revocation, separate registration revocation,
and real collector delivery of transport metadata. A focused typed case additionally
uses the existing author's `PublicCaptureOutbox` and real HTTP collector, capture
replay without duplicate, independent writer/recipient attribution, linked SDK ACK,
notebook output/disclosure projection and revocation withholding. No ordinary generated critic,
planner consumer, inference, host deployment or live two-peer journey is claimed.

## Existing worker-artifact family

This is transport source acceptance, not a generated-critic, independently running
peer, installed-consumer or scientific-validity receipt. The transport never runs
a model, creates output registration, fetches arbitrary peer URLs or carries raw
prompt/tool/output/private reasoning text. Legacy three-field reference envelopes
retain their reference-only partial status and `verified:false` artifacts.

## Wire and authority contract

Use the existing official `a2a-sdk[http-server]==1.2.1`, A2A 1.0 JSON-RPC
`SendMessage`/`GetTask`/`CancelTask` operations. The optional closed fourth field
`generated` on the existing `{operation, reference, authority}` data part pins:

- `record_id`, `collector_sequence`, `critic_credential_id`, recorded `model_id`;
- `artifact_id`, `manifest_id`, `manifest_digest`, `operation_id`, central `task_id`;
- `run_id`, `session_id`, `harness_instance_id`, `context_id`, `recipient_context_id`;
- `context_digest`, `skill_digest`, `config_digest`, `content_digest`;
- `source_event_id`, `source_sequence`.

IDs are opaque, bounded strings; digest pins are lowercase SHA256 hex except
`content_digest` (`sha256:<hex>`). Wire sequences are exact positive integers up
to `2^53-1` because SDK protobuf Struct carries JSON numbers as doubles. No
unknown keys or embedded content are supported. `reference` equals `artifact_id`.
The full existing narrowed delegated authority binding is unchanged: current
parented, active grant; exact sender issuer/tenant/project, receiving credential,
destination/model, zero budget, depth zero, `attempt:status` only, revision/epoch,
central task/variant/execution pins and expiry. This is transport/status authority,
not execution admission or a reasoning grant. Human approval cannot be inferred
from an A2A message.

Before every generated submit/result/consume, the receiving peer uses its own
enrolled control credential to read exactly one collector sequence through
`/projects/{project}/agent-notebook?after_sequence=sequence-1&limit=1`. It requires
the exact record, critic actor/credential, succeeded attempt, generated-output
public harness metadata, bounded capture and matching immutable task/context/run/
session/instance/skill/config/source pins, artifact/manifest/operation and digest.
The collector's revision-3 validation is the public-registration boundary; a
caller boolean or digest alone is never proof. Public visibility/model generation
remain attributed claims, not independent transport verification.

The peer additionally reads `/agent-contexts/{recipient_context_id}` with the
**receiving planner's** credential. The existing backend must allow this exact
project/credential/destination artifact disclosure and validate current custody,
placement and hash. Artifact/manifest/manifest digest/content SHA and explicit
disclosure approval must match. Artifact size is capped at 64 KiB; each registry
HTTP response is capped at 256 KiB, with a five-second timeout and no redirects.
The context route currently returns disclosed artifact content; the adapter does
not persist, transmit or log that content or its body. Enrollment requires explicit
`authority:consume`, `notebook:read`, `context:read`, and (for outbox collection)
`provenance:append` at the configured destination. No owner-session proxy is used.

## Delivery, ACK and restart

The critic sends `submit` then `result` with identical frozen `generated` pins and
authority, stable message IDs, and the durable SDK task ID on result. The result
artifact contains only registration/reference pins, `verified:false` and
`registered_self_reported_generated_output` attribution. Its task stays
`INPUT_REQUIRED` / `awaiting_planner_consumption`, rather than equating transport
delivery with planner consumption. Registered artifacts must exist **before** this
flow; the adapter cannot manufacture critic output while registration is missing.

The receiving planner separately authenticates to this receiving peer using an
enrolled A2A identity whose `agent_id` equals the configured receiving principal.
Only that identity can read the generated task via `GetTask` across its original
critic-owned task scope and send `operation:consume` on the same task with the
same frozen pins/authority and a stable ACK message ID. Sender/third-party/other-
tenant ACKs fail. Grant and current registration/disclosure are revalidated; the
original critic issuer is checked against the persisted task, not a caller's
claimed identity. The atomic ACK stores planner identity/message/pins, history,
event, idempotency and outbox, then completes the task. This is an authenticated
planner **report** of consumption, not evidence of model attention/understanding.

Repeated exact sends/ACKs replay without new transitions/outbox records. Changed
IDs/pins/authority, a second non-replay result or ACK, cancellation, expired/revoked
grant, withdrawn disclosure or unavailable/mismatched registration fail closed.
Restart retains original task/run/session/output and ACK IDs. Cancellation remains
available to the original authenticated sender and fences future consumption.
Old task/outbox history is not rewritten to recover new authority.

Transport outbox records link the registered central `provenance_record_id`, with
null model attribution. The explicit ACK records the planner as remote actor and
causes from the result message. Existing metadata drain and receipt matching are
unchanged; denied/unknown delivery remains pending, without claiming completeness.

## Evidence and integration prerequisites

Focused tests execute official SDK routes and real temporary SQLite ledgers with
**synthetic** control-registration/authority fixtures. They check immutable pins,
public/disclosure/identity mismatch rejection, restart/replay, sender cancellation,
explicit planner ACK, and atomic content-free correlated outbox. They do not prove
real public harness capture, model generation or independent live peers.

Remaining integration: the runtime must collect/register an actual generated
public critic artifact using the existing output/public-metadata contract; a human
must issue critic capture policy and planner artifact disclosure; the planner
consumer must authenticate and perform the explicit ACK; independently running
installed peers must deliver/consume it and join the real notebook. No generated
output or live inference is manufactured by this transport implementation.
