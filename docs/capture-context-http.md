# Authenticated separate input/capture-policy resolution

POST `/projects/{project_id}/agent-capture-contexts/resolve` is the native source
boundary for a production research consumer to obtain actual curated input and a
separate human-issued public-capture policy. Use a valid enrolled external-agent
Bearer credential plus its exact `X-Agent-Destination`. No human proxy, runtime
credential copying, idempotency key, or caller-supplied issuer approval is used.

## Closed request

The JSON body is `ResolveCaptureContexts` (in `capture_context_models.py`): the
closed `CaptureBinding` with **required**, all-or-none separation fields:

- `schema_version: tessera-public-capture-binding-v1`
- `org_id`, `project_id`, `actor_principal_id`, `credential_id`,
  `destination_digest` (`sha256:` plus 64 lower-case hex characters)
- `run_id`, `task_id` (nullable), `context_id`, `context_digest`, `skill_digest`,
  `config_digest`, `allowed_public_events`
- `model_id`, `session_id`, `harness_instance_id`
- `input_context_id`, `input_body_digest`, `capture_policy_context_id`

`context_id == input_context_id`, `context_digest == input_body_digest`, and the
input and policy context IDs must differ. Content digests are bare 64-hex SHA-256,
unlike the prefixed destination digest. IDs/events/digests retain the existing
bounded binding validators. Extra fields, including issuer-role/approval booleans,
are rejected. The path project must equal the body project.

## Closed response

`CaptureContextsReply` returns:

```
{
  "verified_binding": <the complete validated request binding>,
  "resolution": {
    "input_context": <ResolvedCaptureContext>,
    "capture_policy": <ResolvedCaptureContext>
  },
  "verification_scope": "current_issuer_context_policy_and_input_digest",
  "authority_effect": "none"
}
```

Each `ResolvedCaptureContext` contains only `context_id`, `org_id`, `project_id`,
`credential_id` (recipient), `destination_digest`, `issuer_principal_id`,
`issuer_role:human`, `disclosure_approved:true`, `classification` (public/project),
and its actual bounded immutable `body`. No tokens, credential hashes, raw logs,
hidden reasoning, membership list, or general context catalog is returned.
Successful responses are `Cache-Control: no-store`; consumers must not cache the
result as continuing execution authority or log the input/policy bodies.

The HTTP port should parse this reply, verify `verified_binding` equals its pinned
binding, and pass `resolution` to the existing
`validate_separated_resolution(binding, resolution)` consumer contract. Actual input
delivery uses `resolution.input_context.body`, **not** the policy JSON or a digest
echo. The independent policy body is
`tessera-separated-public-harness-disclosure-v1`; all its run/task/input/model/
session/harness/skill/config/event pins must match the binding. This endpoint does
not mint a grant, approval, capture event, session, or model-output receipt.

## Current authority and snapshot semantics

The route opens one SQLite read transaction, invokes the existing transaction-local
`resolve_separated_capture_contexts(connection, authenticated_actor, binding)`,
then closes the connection on success or failure. The resolver rehydrates the actor
and both human issuers from actual current principal/credential records. It checks
tenant/project/recipient/destination, `context:read` **and** `provenance:append`,
ordinary context bodies, current issuer role/liveness, approved disclosure,
classification, actual input bytes and separate policy pins. `created_by` alone
is not treated as human proof. Artifact contexts are not silently substituted.

Ordinary context and enrollment records are immutable, with approved disclosure
required at issuance; there is no separate per-context revoke flag in this existing
schema. Current issuer or agent revocation/expiry invalidates this read. This adds
no context revocation API or schema migration. Missing or wrong-scoped contexts
fail closed. Revocation committed before the snapshot denies access; a concurrent
commit after the snapshot is observed by the next request. A returned snapshot
does not grant later actions or supersede their own current-authority checks.

Malformed/missing/extra pins: 422. Binding/content/policy mismatch: 409. Forbidden
scope, destination, issuer role/liveness, wrong context recipient: 403 (issuer
liveness failure is sanitized by the existing resolver). An invalid/revoked/
expired caller credential is 401; inaccessible projects retain existing 404
semantics. Errors return no partial input or policy.

## Verification boundary

`tests/test_capture_context_api.py` exercises real temporary authenticated HTTP and
SQLite, synthetic curated research text and policy, immutable issuance, issuer and
agent revocation/expiry, role changes, actor/tenant/destination/action fences,
digest/policy mixups, repeated side-effect-free reads and a concurrent revocation
snapshot. `tests/test_separated_capture_contexts.py` owns the reused resolver's
deeper context/output/provenance checks. No installed state, auth files, live
reasoning, host, runtime service, MCP or A2A acceptance is claimed. Runtime-owned
HTTP port/factory integration and real planner/critic admission remain separate.
