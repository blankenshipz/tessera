# Generated public research output (intent revision 3)

This is registration/storage authority, not inference proof, execution approval,
Job output, or scientific assessment. The authenticated agent is the recorded
writer. Human-admitted model/session/run/instance/context/skill/config pins and
public-source IDs remain attributed claims, not independent model verification.
Never submit hidden reasoning, raw prompts, raw tool arguments or private logs.

## Human admission, registration and lookup

1. Human creates an ordinary `/projects/{project}/agent-contexts` disclosure
   for the actual enrolled agent credential/destination. Sender enrollment requires
   `context:read` and `provenance:append`; no owner proxy.
2. Human POSTs `/projects/{project}/generated-output-grants` with:
   `{action:"generated-output:register", output_kind:"planner_proposal"|"critic_review",
   credential_id, destination, pins,
   public_output_admitted:true, max_outputs:1..16, max_total_bytes:1..1048576,
   expires_at:<offset timestamp within 24 hours>}`. This does not permit execution.
3. Agent POSTs `/projects/{project}/generated-outputs` with a stable
   `Idempotency-Key`, `{grant_id, expected_revision:1, pins, source_event_id,
   source_sequence, output_kind:"planner_proposal"|"critic_review",
   visibility:"public", sha256:<hex>, content_base64:<UTF-8 bytes>}`.
   Bytes must be nonempty, at most 64 KiB, with exact digest. Output and byte
   budgets, immutable bytes, attribution, event and idempotency commit atomically.
   Identical replay preserves the output ID without another budget charge;
   changed public-source identity under the same key or rekeyed duplicate fails.
4. Human POSTs `/generated-outputs/{id}/disclosures` with `{credential_id,
   destination, disclosure_approved:true, expires_at}` for the receiving planner.
5. Recipient GETs `/generated-outputs/{id}?disclosure_id={disclosure_id}` with its
   own enrolled `context:read` credential and destination. Human may GET without
   disclosure. Returned body is bounded and includes immutable `pins`, source IDs,
   `artifact_id`/`id`, `content_digest` (`sha256:<hex>`), hash/size/content_base64,
   actual writer actor/credential/org, `scientific_validity:not_assessed`,
   `execution_established:false`, `custody_state:human_admitted_public` and
   `attribution:registered_self_reported_generated_output`.
   GET `/generated-outputs/{id}/metadata?disclosure_id={disclosure_id}` applies the
   same current access/integrity checks but omits `content_base64` for A2A/metadata
   lookup. It does not waive human disclosure merely because bytes are omitted.

`pins` is closed: `model_id`, `run_id`, `session_id`, `harness_instance_id`,
`context_id`, SHA256-hex `context_digest`, `skill_digest`, `config_digest`, plus
nullable `task_id`/`operation_id`. Task/operation, if supplied, must be registered
and project-matching; none is fabricated when absent. Human approves the curated
digest pins; no runtime inference permission or monetary spending is issued here.

Human POSTs `/generated-output-grants/{id}/revoke` or
`/generated-output-disclosures/{id}/revoke` with `{expected_revision}` and stable
idempotency key. Revoked/expired grants block new registration **and replay**;
revoked/expired disclosures block recipient reads. Recipient credential revocation,
tenant, destination and enrollment are rechecked. Already registered bytes and
content-free event history are retained; human read remains a separate boundary.
An output is not automatically disclosed even to its author. Human review stays
in the existing separately authenticated review/promotion boundary.

## Storage and schema

Schema **53** adds `generated_output_grants`, append-only `generated_public_outputs`
and `generated_output_disclosures`. Immutable bytes use the ordinary bounded
SQLite artifact-byte persistence pattern in a distinct table: the legacy worker
`artifacts` table requires execution grant/lease/worker IDs and is NOT populated
with fabricated identity. No filesystem external effect is made inside the write
transaction. There is no worker manifest, Job UID, provider receipt or fake run.

Explicit `tessera migrate --state-dir <state>` upgrades 52→53. Historical schema
52 receipts remain historical. Migration creates empty ledgers, does not rewrite
history or infer grants/disclosure; only active backup-success freshness is reset,
and held-restore forensic metadata/write fences are preserved. No installed state
was migrated by this implementation worker.

## Consumer integration required

Source-54 now reports a typed local path joining this registered output family to
public-capture/provenance notebook records and A2A author/registration plus five
receiver transport records. This supersedes the earlier source-integration gap for
that bounded component path; its model bytes and HTTP/SQLite effects are synthetic
fixtures, not generated model output or independently running peers. Preserve
narrowed transport authority and never synthesize a worker `manifest_id`, Job UID,
or worker artifact for public research output. Backend events contain no output
bytes; the HTTP registration response also omits them. Only the separately
authorized lookup returns content. The V2 runtime capture/consumer loop, real
human review, independently running planner/critic and end-to-end live delivery
remain unimplemented or unverified. Neither transport nor this registry establishes
reasoning or scientific validity.

Tests are fixture output registration through actual temporary HTTP/SQLite;
they are not generated planner/critic or live-inference evidence.
