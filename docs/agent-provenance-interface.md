# Agent provenance interface (intent revision 1)

## Revision 3 schema53 research-output family

The collector now admits a distinct optional `record.generated_output` reference,
validated transaction-locally against actual `generated_public_outputs` registration,
human admission and current recipient disclosure. The authoritative field contract
and adapter/outbox integration are in [public-capture-interface.md](public-capture-interface.md).
This research family has no worker manifest, Job UID, lease or invented task/operation.
It uses explicit `family:generated_public_research_output_v1`, registered ID/hash/kind,
real writer identity and an independently human-issued output disclosure. Absent fields
stay off the wire; legacy worker references/digests/replay remain unchanged. The older
manifest-specific generated-output guidance below applies only to worker-manifest
output, not human-admitted public planner/critic research output. Actual V2 source
capture/consumer binding remains outside this lane and unverified.

## Revision 3: public harness metadata contract (source-only acceptance)

This continuation owns provenance modules/models/API/tests only. Schema 52,
runtime consumers, migration/store bootstrap, host/auth state and prior receipts
are unchanged. No runtime public events or generated output are claimed by the
collector fixture tests. Prior sections below preserve historical interfaces.

Optional `record.harness` carries the closed
`tessera-public-harness-metadata-v1` shape: stable `run_id`, `session_id`,
`harness_instance_id`, actual central `task_id`, `context_id`, immutable approved
`context_digest`, `skill_digest`, `config_digest` (64 lowercase SHA256 hex),
`public_event` (message/tool/permission/delegation/generated_output),
`source_event_id`, `source_sequence`, `source_watermark`, `capture_state`
(bounded_public_stream/unknown) and at most 16 closed gap ranges.
No message text, prompt, tool arguments/results, exception, private reasoning,
arbitrary metadata or self-asserted public/approval flag is supported.

The disclosure boundary is a separately human-created immutable agent context,
not a caller's `public=true` assertion. Its ordinary `body` stores strict JSON
`tessera-public-harness-disclosure-v1`: run/task IDs, the three digests, allowed
public event categories. Its existing credential/project/destination and explicit
disclosure-approved/classification fences still apply. `references.context_id`
and `references.task_id` must match the harness metadata. Actual public visibility
and installed harness binding remain attested, not independently proved by these
pins. Permission observations create no Tessera approval or execution authority.

Public generated-output metadata additionally requires the existing exact
`output_manifest_id`/`artifact_id`/`operation_id`, `output_proof`, current explicit
artifact disclosure and matching artifact SHA256 as `content_digest`. Missing
central output registration is an implementation gap for the future runtime, not
permission for the collector to create a fake artifact or accept raw generated
text. A source event is unique within its actor/project/run; session/instance and
context/config/skill pins cannot change within that run. Stable record/key replay
retains the original actor and does not acknowledge new source events implicitly.

Source sequence numbers describe the approved public stream, not hidden reasoning.
Notebook projection reports observed collector counts versus the reported
source watermark, explicit source gaps and unknown capture states separately from
the ordinary control/provenance watermarks. A contiguous recorded public stream
through an attested watermark is not a complete raw transcript, scientific proof,
or evidence about uninstrumented/off-platform work. Optional absent harness fields
must stay off the wire to preserve pre-revision-3 request digests and acknowledgements.

Future runtime/A2A consumer contract (implementation outside this lane):

1. A separately authenticated human creates the ordinary agent context whose
   `body` is `PublicHarnessDisclosure` JSON. No new enrollment action is required;
   existing `provenance:append`/`notebook:read` and destination scopes still apply.
   Match actual curated context, skill and secret-free harness configuration pins
   before capture; the collector matching a reported pin is not proof the harness
   installed or exercised that configuration.
2. Add `PublicHarnessMetadata` as optional `AgentProvenanceRecord.harness` on the
   existing `AppendAgentProvenance.record`. Existing POST/Idempotency-Key/body cap
   and grant/output proof shapes are unchanged. Keep record/key, run/session/
   harness instance and source event/sequence stable through outbox restart.
   `source_sequence` describes eligible approved public metadata only: do not
   inspect/count private reasoning parts as public capture or gaps.
3. Map `public_event` to record kind: message->message, tool->tool,
   permission->permission, delegation->delegation, generated_output->attempt.
   Messages carry `remote.message_id`; tools carry `tool_id`. Permission/delegation
   observations are self-reported and mint no authority. `references.context_id`
   and central `references.task_id` must exactly match harness context/task pins.
4. SHA256 pins are 64 lowercase hex; `content_digest` retains `sha256:<64hex>`.
   Public event content digests, including messages, are accepted only for an actual
   registered, disclosed artifact with matching task/operation/manifest references
   and existing `output_proof`. Otherwise emit IDs/outcomes only, not hashes of
   whole prompts/tool payloads or raw/generated text disguised as public metadata.
5. Gaps are at most 16 ordered nonoverlapping `{first,last,reason}` ranges within
   the reported watermark, never covering the recorded event itself. Reasons are
   source_unavailable/redaction_withheld/delivery_unknown/unsupported_public_event.
   Source/sequence counters are strict positive integers <= 2^63-1. Capture state
   is bounded_public_stream or unknown; callers cannot assert complete transcripts.
6. Notebook `provenance[].harness_capture` independently exposes
   `reported_source_watermark`, `recorded_source_events`, missing-event count,
   gap-report/unknown counts and collector sequence watermark. Completeness is
   unknown_source_capture/reported_source_gaps/missing_source_events or
   complete_recorded_public_metadata_through_reported_watermark. Declared historical
   gaps/unknown reports remain conservative accounting limitations; no implicit
   recovery/resolution or off-platform completeness is invented. These are
   same-snapshot counts across the recorded source, even on a paginated projection.

Validation is transaction-local inside `record_agent_provenance` via
`validate_public_harness_record`; no new database tables/migrations or central
`store.py`/`agent_provenance_store.py` changes are needed. Existing contexts without
this strict human-issued JSON policy remain valid legacy context, but cannot
authorize a new harness/public-data claim. Absent harness data stays absent on
wire and is never relabelled as a captured public stream. Real public visibility,
event coverage, generated reasoning and planner/critic integration remain pending
consumer implementation and real acceptance; fixture records do not prove them.

## Output-ledger correlation continuation

The sanitized installed run5 receipt reports a 36-byte retained output, manifest
`artifact_manifest_f7176b569cf3ad8e47d0b210e51a98a8`, operation
`5eec1cadfab46be42210a9661b128312`, Job UID
`6fbfa403-6481-4dc6-a004-32982777474f`, `late=true`, quarantine retained,
custody released, and control resolution `cancelled`. This is inspected receipt
data, not independently rerun execution, and the numeric output is not scientific
success. The existing worker manifest event uses generic `id`, not a typed
manifest reference; the notebook currently drops that link. Central operation
provider identity is a Kubernetes UID hash, not a recoverable raw Job UID.

Continuation adds `references.output_manifest_id` and `provider_job_uid`, and
closed `late`, `quarantined`, `cancelled` provenance outcomes (legacy `canceled`
remains supported). Artifact hashes, manifest digest, late/quarantine state,
attempt/operation resolution and human custody/disposition are projected only from
the same central snapshot, never from arbitrary event payloads. Historical
manifest events are resolved through the actual immutable manifest row, including
their existing generic `id`, without claiming full historical instrumentation.

Agent output details additionally require current explicit artifact disclosure
to that same credential/destination and matching active retained placement,
current released custody and retain-quarantined disposition. Notebook scope alone
does not release artifacts. Withdrawn/restricted history has an explicit withheld
marker, no artifact hashes or bytes. Human authorized metadata projection retains
late/failure/cancellation even without researcher byte-read authority.

Raw Job UID cannot be inferred from a provider hash. New optional append
`output_proof` names the manifest and pins its digest; optional `provider_job`
contains opaque cluster/namespace/Job UIDs. Its canonical Kubernetes provider hash
must match the operation's actual recorded provider identity before an append-only
central binding is persisted. An absent binding projects Job UID as unknown, not
the receipt's unverified assertion. Backend worker/collector integration will use
the published transaction-local helper below; no provider or Kubernetes calls are
performed by notebook projection.

| Check | Assigned journey/proof | Remaining owner/prerequisite |
| --- | --- | --- |
| notebook | Authorized same-snapshot output/hash/attempt/operation projection, honest late/quarantine/cancel/unknown labels, disclosure withdrawal and bounded replay; isolated registered-output SQLite/HTTP tests | Provenance lane source checkpoint; no installed execution asserted |
| integrated-journey | Installed operator/agent notebook correlates the run5 manifest without converting late output into success | Parent/live journey child: installed additive schema/source, existing authorized read references, current artifact disclosure for agent detail |

### Exact central integration and installed assertion

`create_agent_provenance_schema(connection)` now invokes additive
`create_output_provenance_schema` (new immutable
`agent_output_provider_bindings`). Existing-state upgrade must install it before
the updated projection runs; no version renumbering or historical binding synthesis.
No event writer change is required to correlate existing manifest events: the
projection resolves `worker.artifact_manifest_quarantined.payload.id` through the
project-owned manifest row, and similarly joins registered operation events.
`event.output` and `provenance.output` contain current-snapshot ledger metadata,
not a rewrite of the original event's state at its timestamp.

The exact transaction-local provider helper is
`record_output_provider_binding(connection, actor, project_id, manifest_id,
provider_job)` from `agent_provenance_outputs.py`. `provider_job` is the closed
`{cluster_uid, namespace_uid, job_uid}` shape; the helper authenticates the real
actor, project and original worker (or scoped/disclosed agent), checks the recorded
operation's provider hash, appends once and does not commit. Backend owner can add
an optional validated provider-job proof to the ordinary worker manifest intake
and call this helper after insertion/before the existing manifest event/commit.
Do not substitute a receipt's Job UID or recover it from the provider hash.
Alternatively a properly enrolled/disclosed agent can use the existing append
route with `output_proof` and exact manifest digest. An optional asserted
`references.provider_job_uid` must match that binding; verified central fields are
joined separately rather than rewriting the reporter's request references.

Output proof example (identifiers from authorized runtime references, not secrets):
`{"record":{"record_id":"stable_output_report_id","kind":"attempt",
"outcome":"late","correlation_id":"stable_operation_correlation",
"references":{"output_manifest_id":"actual_manifest_id",
"artifact_id":"explicitly_disclosed_artifact_id"}},"output_proof":{
"output_manifest_id":"actual_manifest_id","manifest_digest":"actual_64hex_digest",
"provider_job":{"cluster_uid":"observed_uid","namespace_uid":"observed_uid",
"job_uid":"observed_uid"}}}`. Omit provider proof when unknown; never rekey a
rejected outbox to hide a digest/revocation/disclosure denial. Absent new output
reference fields serialize without nulls to preserve older outbox wire digests
and exact acknowledgements. Legacy agent exports still need notebook:read.

After installing/migrating and draining the authorized notebook/export pages, the
live journey child should locate a `control_events` item with
`event_type == "worker.artifact_manifest_quarantined"` and
`output.output_manifest_id == "artifact_manifest_f7176b569cf3ad8e47d0b210e51a98a8"`.
Assert its registered operation is `5eec1cadfab46be42210a9661b128312`, manifest
digest `dbbc19a5189b39244b5a474a59ae19e50eff8d7adfd86b9e8bc3b83918780a05`, and an
authorized artifact has SHA256
`db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`, size 36.
Assert non-null actual admitted attempt ID, `late=true`, `collection_outcome=late`,
`status=quarantined`, `control_outcome=cancelled`, `disposition=retain_quarantined`,
`custody_state=released`, `scientific_validity=not_assessed`, and execution is not
established by output metadata. With a validated provider binding, assert Job UID
`6fbfa403-6481-4dc6-a004-32982777474f`/`recorded_uid_hash_match`; without one require
null Job UID/`job_uid_unknown` and report that precise remaining binding gap.
Agent output hashes require current explicit matching artifact disclosure; otherwise
assert `withheld_current_disclosure` rather than bypassing custody. Historical
coverage must continue disclosing unregistered/uninstrumented work as unknown.

The join also exposes manifest/grant/lease/operation timestamps, current fence
epochs, renewal acknowledgement count/first/last times and the first actual central
cancellation-request event. It deliberately labels reason/late cause unknown when
not recorded in closed metadata. The run5 receipt itself lacks those timeline and
reason fields. This leaf has the sanitized receipt but no secure human API base/
credential reference handoff for the optional live read; no permission denial or
new user authorization is asserted. Parent/live child should use their existing
authorized private guide references to inspect this metadata after installation.

## Runtime/A2A append contract (central integration pending)

`POST /projects/{project_id}/agent-provenance`, `Idempotency-Key`, own agent
bearer and `X-Agent-Destination`. Body: `{"record": AgentProvenanceRecord,
"grant_proof": null | {"grant_id": id, "expected_revision": int,
"expected_epoch": int}}`. Requires explicit immutable `provenance:append`
enrollment, never context-read fallback. Maximum body is 16 KiB.
`record.references` are central IDs validated against project-owned records;
at least one durable central anchor is required. `context_id` may anchor an
already-disclosed context belonging to this credential/destination. Grant links
require the proof and exact subject/project/revision/epoch; a revoked grant may
be referenced with its current pins to report denial, without consuming authority.
Remote A2A task/message/peer IDs instead belong in `record.remote` (`task_id`,
`message_id`, `peer_id`), explicitly self-reported assertions scoped to the enrolled
destination. Do not put remote IDs in central references.
The writer derives `remote.destination_digest` from enrolled destination and
stores grant revision/epoch pins in the record; supplied contradictory attribution
is denied. Destination digest is correlation metadata, not a secret-scanning guarantee.
No central approval, decision, grant issuance, or verified-execution claim can be
created by this API.
Model IDs are self-reported. Outboxes retain the stable record_id and idempotency
key; retry after reconnect with the same body. Stale pins require a new explicitly
correlated record, never mutation of history. Endpoint only records metadata;
it does not dispatch work, consume grants, or attest scientific validity.

Central owner registers `agent_provenance_api.router` and
`app.state.agent_provenance = AgentProvenanceStore(state_dir)` from
`agent_provenance_store.py`; add `provenance:append` to the human-issued enrollment
Literal/list maximum. Existing provenance schema bootstrap remains required.

Owned additive files: `agent_provenance.py`, `agent_provenance_models.py`, and
`tests/test_agent_provenance.py`. Existing schema/store/API/export files remain
owned by the authority/backend lane. This note defines the integration contract;
passing module tests are not proof of integrated instrumentation or live execution.

- `create_agent_provenance_schema(conn)` executes additive DDL without committing
  or opening a transaction. Call from both new and upgraded schema bootstrap.
- `record_agent_provenance(conn, actor, project_id, record)` requires the caller's
  active transaction and authenticated actor. `record` is `AgentProvenanceRecord`
  or its strict dictionary representation. It rechecks live identity/project,
  derives tenant/actor from that identity, and returns the immutable record.
  No second connection or transaction is opened. Identical `record_id` replay is
  stable; changed replay conflicts. Use stable command-derived record IDs.
- `project_agent_notebook(conn, actor, project_id, *, after_sequence=0,
  after_event_cursor=0, limit=100, max_bytes=1048576)` requires an existing read
  transaction, authenticates a human control principal and returns redacted
  provenance plus ordinary project/control/research/authority event metadata in
  the same snapshot. Add it to the already-authorized notebook/export transaction,
  not after the transaction closes. It is project-scoped, not a complete study export.
- `project_external_agent_notebook(conn, actor, project_id, *, after_sequence=0,
  after_event_cursor=0, limit=100, max_bytes=1048576)` is the separate agent-facing
  projection. It requires an active transaction, a live control-audience `agent`
  credential bound to that exact project, immutable external-agent enrollment with
  explicit `notebook:read`, and `actor["agent_destination"]` matching the enrolled
  destination. Human, worker, unenrolled and context-only identities are denied.
  Expiry, revocation, parent-credential and tenant fences are rechecked by the live
  credential verifier on every call. Output and caps match the human projection;
  no identity is substituted and the human helper remains human-only.

Records carry typed event kind/outcome, correlation/causation IDs, approved opaque
record references and SHA-256 digests. Model/tool identity is an opaque identifier;
model attribution is self-reported, never independent model verification. Prompt,
message/tool content, exception strings, arbitrary metadata, secrets and hidden
chain-of-thought are not accepted. Permission/message/delegation/attempt/grant/
operation links use named reference fields, not arbitrary payloads.
Opaque identifier syntax is not a secret scanner or reference-existence proof:
integrating code must supply authorized registry identifiers, never encode secret
material in an identifier. Links are attributed assertions until the authorized
record projection resolves them; they are not independent verification.

Backend hook: write within successful admission/consume/delegate transactions,
after result IDs are available and before commit. Denied/failed command transactions
roll back: persist a minimal denial/unknown record only through a separately
authorized audit transaction after rollback, never commit a failed command to save
its provenance. Reference the minimal audit event where available. An unknown
remote outcome remains unknown until an appended causal resolution record.

Watermarks are independent durable provenance sequence and ordinary event cursor.
Pagination is explicit and both streams must be drained. Completeness means only
the recorded project metadata snapshot; historical/uninstrumented/off-platform
work remains unknown. Projection cannot certify experiment execution, scientific
validity, or full prompt/log capture. Joined event payloads retain only validated
opaque identifiers and digests, not arbitrary event content. Integration must not
replace existing authorized research records/byte caps or label this a complete
study ledger. Include this section in research and experiment exports using the
existing snapshot and include its bytes in the export's total byte cap.

## Precise backend integration steps

1. Import `create_agent_provenance_schema` inside `store._create_schema` alongside
   the other additive schema helpers and invoke it after project/principal tables
   exist. Also call it in the owned additive upgrade path for existing databases;
   no existing migration numbering should be replaced. The function does not
   commit, including when installed in an active migration transaction.
2. In authority successful command callbacks, call e.g.
   `record_agent_provenance(connection, actor, project_id, {"record_id":
   "provenance_" + consumption_id, "kind": "permission", "outcome": "succeeded",
   "correlation_id": command_id, "references": {"grant_id": grant_id,
   "consumption_id": consumption_id, "attempt_id": attempt_id}})`.
   Omit absent references or use null. Use actual command lineage, not a new random
   correlation per replay. Delegations use `kind="delegation"`; denied requests
   use `outcome="denied"`, a closed error code and the minimal audit transaction.
3. At `ResearchStore.export_study` and `ExperimentStore.export_experiment`, after
   live identity/resource authorization and inside the existing read transaction,
   attach `project_agent_notebook(connection, actor, project_id)` as an explicitly
   project-scoped notebook section. Include its serialized bytes in the existing
   aggregate cap. For larger notebooks either fail closed with a bounded export
   limit, or drain both cursors in that same snapshot; never silently truncate and
   preserve a complete-study claim. Metadata is a complement to, not replacement
   for, research/experiment records and authority consumption detail.
4. Add integration tests for old-state bootstrap, authority/admission atomicity,
   correlated consumption-to-attempt links, denial survival after command rollback,
   authorized export byte accounting and study/project scope disclosure. Module
   tests cover real local SQLite behavior only; no live runtime proof is claimed.
5. Backend-owned enrollment action Literal must add `notebook:read` and adjust
   its actions-list maximum for the complete supported union. Existing enrollments
   gain nothing automatically; immutable scope rows may contain this action only
   after explicit human enrollment. No new provenance schema is required for this
   additive action. Agent HTTP notebook/export routes must authenticate the agent's
   own bearer, carry `X-Agent-Destination` as the untrusted `agent_destination`
   claim, open a read transaction, and call `project_external_agent_notebook`.
   No fallback to `context:read` or owner-token proxy is allowed. This grants only
   redacted project notebook metadata, not arbitrary artifact bytes or research
   content: any broader export needs its own existing resource permissions and
   aggregate byte accounting. Routes and enrollment changes remain backend-owned.
