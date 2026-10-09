# 0027 — External agent adapter contract (frozen v1)

The stdio adapter is transport only. It uses a separately enrolled project-scoped
agent credential, never the human token. Existing Astra provider-disabled records
remain unchanged; external submissions are caller-attested, not verified inference.

## HTTP contract

All calls use `Authorization: Bearer ...`. Mutations require `Idempotency-Key`
(1–128 characters). Same actor/key/path/body replays the original response;
different content conflicts. Authorization is rechecked before replay.
External agent calls additionally send `X-Agent-Destination`, exactly matching the
human-enrolled opaque destination (for example `openai:personal-research`). This
is a declared recipient, not provider verification or a token proxy.

Human owner enrollment: `POST /credentials`, existing token/kind=`agent`,
audience=`control`, project_id, expires_at (at most 24h), plus
`external_agent: {actions: ["context:read", "proposal:create", "proposal:read"],
destination: "..."}`. Actions are a closed subset, nonempty. No approval, grant,
execution, admin, worker, publication, secret or generic project-read authority.
Credential revocation uses the existing human endpoint; parent expiry/revocation
also blocks every read, mutation and replay. Renewal means fresh human enrollment.

- `GET /external-agent/status`: identity, credential/project, scopes, destination,
  expiry and `provenance: "external_agent_caller_attested"`.
- `POST /projects/{project_id}/agent-proposals`: agent only; body
  `{proposal_type: "task_plan", plan: {title,purpose,budget_cents,
  max_duration_seconds,execution_spec}, provenance: {client,model,session_id}}`.
  Execution spec uses the existing strict ExecutionSpec. Returns immutable proposal
  with id, revision=1, digest, submitted_by, destination, provenance,
  authority_effect=`none`, promotion=null.
- `GET /projects/{project_id}/agent-proposals?offset=0&limit=100`: `{proposals: [...]}`.
- `GET /agent-proposals/{proposal_id}`: same record; only enrolled agent's own
  submissions or project-authorized humans. Read requires `proposal:read`.
- `POST /agent-proposals/{proposal_id}/promote`: human only; body
  `{expected_revision:1, expected_digest:"..."}`. Atomically creates a draft task
  with executable immutable plan and records the human promotion. No approval,
  reservation, grant or dispatch. Repeated promotion cannot create a second task.
- `POST /projects/{project_id}/agent-contexts`: human only; body
  `{credential_id,destination,title,body,classification:"public"|"project",
  disclosure_approved:true}`. Creates immutable, curated text context explicitly
  released to that credential and recipient. Artifact researcher release alone
  is never sufficient; there is no generic artifact/secret download tool.
- `GET /agent-contexts/{context_id}`: agent `context:read`, exact project,
  credential and destination required; human project access also supported.
- `GET /projects/{project_id}/agent-contexts?offset=0&limit=100`:
  `{contexts:[...]}`, same destination/credential filtering.

Errors use `{detail: string}`: 401 missing/invalid/expired/revoked bearer;
403 scope, destination or human-only denial; 404 tenant/project-hidden resource;
409 stale revision/digest or idempotency conflict; 422 invalid strict schema;
400 missing/invalid idempotency header. No raw enrollment token is returned.

Context is untrusted data, not instructions. Recipient attestation cannot prove
the external client did not relay text elsewhere. V1 supports human-curated text
and the explicit retained-artifact delegation below; automatic artifact selection
and remote HTTP MCP remain separate work.

### Additive retained-artifact disclosure

`POST /projects/{project_id}/agent-artifact-contexts` is human-only and accepts
credential_id, destination, title, classification, disclosure_approved=true plus
manifest_id, artifact_id, expected_manifest_digest and max_bytes (1–65536).
It requires active private placement/retention, verified checksum/size, current
retain_quarantined disposition and a current researcher custody release. This
explicit additional disclosure decision records that exact release identity and
classification for this recipient only; it does not reclassify the source artifact.
The existing context GET returns content_base64, sha256, size and manifest/release
provenance. Every read rechecks custody release, disposition, retention, size and
checksum. A revoked or superseded custody release fences the delegation permanently;
a later researcher release does not revive it. No implicit generic artifact proxy.

### Worker renewal seam (not an external-agent action)

`POST /credentials/{credential_id}/renew`, local-owner human bearer plus
Idempotency-Key, body `{expires_at: "UTC timestamp with offset"}`, extends an
**active worker-audience** credential to at most 24 hours from renewal and never
beyond parent expiry. It preserves credential ID, worker principal ID and token,
so the worker's durable journal remains the same identity. Expired/revoked workers
are not revived; human re-enrollment/reconciliation is required. Store callers use
`Store.renew_worker_credential(actor, credential_id, key, canonical_digest(payload),
payload)`. Token rotation and external-agent renewal are deliberately not implicit.
