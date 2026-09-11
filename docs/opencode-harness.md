# OpenCode harness adapters

`tessera_worker.opencode_harness` is a synthetic, in-process adapter POC. It has
no HTTP implementation, provider call, credential path, control-plane write,
execution dispatch, artifact handling, or result acceptance.

The adapter accepts only an injected five-method client (`version`,
`create_session`, `send_message`, `abort`, and `events`). Before it starts its
deterministic `tessera-opencode-poc-v1` fixture session, it requires an exact
semantic-version pin and a current-authority callback. A false or failing callback
blocks the start; it does not alter grants, budgets, credentials, or policy.
It rechecks that callback immediately before each privileged upstream boundary:
version inspection, session creation, initial message, and abort. If authority is
lost after creation, the acknowledged session remains recorded but no message is
sent. A lost session-create, message, or abort reply is journaled as uncertain;
the POC reserves the same operation identity and never retries it as a new session
or message identity.

Each accepted event is bounded and carries organization, project, task, operation,
epoch, harness instance, upstream session, cursor, and event identity. Events with
an unknown session or any mismatched binding field are quarantined without changing
the active operation. Cursors are non-negative ordered sequence numbers beginning
at zero. `after_cursor` is exclusive: replay uses only the highest *contiguous*
accepted cursor, never the largest event merely observed. Events arriving ahead of
a gap are buffered and have no prompt, terminal, or other active effect until the
missing events arrive; duplicate event IDs/cursors are dropped, while reuse with a
different cursor/ID is quarantined. A delayed event behind the committed cursor is
also quarantined. The supervisor retains `ReplayState` (binding, contiguous cursor,
seen identities, and cancellation flags) across adapter restart; restore only
replays the existing session and never creates another one. Missing events leave
the cursor at the last contiguous event and remain visibly unreconciled rather than
being skipped.

The synthetic schema recognizes only `status`, `session.terminal`, and the four
prompt kinds above. Any other upstream event kind is quarantined rather than being
silently treated as a status update. Quarantine records only attribution metadata,
not payload content.

Permission, tool, MCP, and provider-auth prompts are only bounded observations and
optional scoped callback requests. Callback results are ignored and never grant an
upstream permission. `abort` records requested/unresolved cancellation; only a
matching authoritative `session.terminal` event with `state: cancelled` confirms
termination. This is not a provider termination receipt.

This evidence keeps OpenCode optional and pinned. A live integration still needs a
selected compatible upstream version, conformance/security testing, enrolled-node
controls, and the existing Tessera authority, dispatch, artifact, cost, and result
acceptance gates. Passing these fixture tests is adapter evidence only, not pilot
completion.

## Live pinned HTTP/SSE seam

`tessera_worker.opencode_http` and `tessera_worker.opencode_live_adapter` add a
separate live transport seam, exercised only against an in-process fixture so far.
It accepts exactly one configured authenticated endpoint, an exact semantic
server-version pin, and the SHA-256 of the canonical pinned `/doc` OpenAPI
response. It verifies both before session mutation and before each independently
opened event stream. The endpoint must use
TLS, unless it is a literal `127.0.0.1` or `[::1]` HTTP origin; endpoint paths,
credentials in URLs, redirects, proxy environment settings, cookies, and general
OpenCode routes are rejected or unavailable. The endpoint authorization value is
an endpoint-only Bearer header, never a message part, journal value, or provider
credential.

The client exposes only `POST /session`, `POST /session/{id}/message`, `POST
/session/{id}/abort`, and `GET /event` after the pinned `/doc` inspection. It
sets bounded request/response/event sizes and accepts no transport timeout greater
than 30 seconds. The adapter only emits known typed `/event` envelopes for its
already attributed session; unknown, foreign, malformed, secret-bearing, oversized, and
out-of-order events are quarantined. Numeric SSE IDs are exclusive replay cursors;
gaps are retained, duplicate/reused IDs are deduplicated or quarantined, and a
reopened local journal resumes with `Last-Event-ID` at the contiguous cursor.

The private `0700` local journal stores operation binding, session identity,
effect intent/outcome, cancellation state, and safe pending event metadata. A
session identity cannot bind more than one operation. The journal persists
create/message/abort intent before the outbound call and leaves a lost
reply `uncertain`; a restart cannot create a second operation identity. A
supervisor authority callback fails closed before contract inspection, create,
message, abort, and event-stream calls. Abort acknowledgement remains only an
OpenCode acknowledgement: provider termination, costs, output acceptance, and
scientific success remain unresolved. The fixture prohibits provider routes and
does not enable OpenCode shell, MCP, skills, permissions, provider authentication,
or model calls.

## Opt-in real-server compatibility fixture

`scripts/check_opencode_compatibility.py` is a separate node fixture, not a
replacement for the synthetic adapter or a dispatch path. It runs only when all
three exact pins are configured: `TESSERA_OPENCODE_EXECUTABLE` (an absolute,
non-symlink executable), `TESSERA_OPENCODE_VERSION`, and
`TESSERA_OPENCODE_SCHEMA_SHA256` (the canonical JSON SHA-256 of that version's
`/doc`). It never installs OpenCode or accepts provider credentials. No opt-in
emits JSON `status: skipped`; an incomplete, unexecutable, or version-mismatched
pin emits `status: unavailable`. Neither is compatibility evidence or a passing
result, and both have nonzero CLI exits.

The fixture creates one temporary loopback server with a private temporary HOME,
config, and workspace. Its clean environment drops inherited OpenCode state,
provider credential variables, and proxies. It writes a provider-empty config,
disables updates/plugins/MCP/LSP/formatters/snapshots/sharing, and denies all
permissions/tools. The generated server-only HTTP Basic credential is used only
to test the upstream server's documented fixture authentication mechanism; no
Tessera, provider, shell, MCP, OAuth, or credential API is used. It uses literal
`127.0.0.1`, no DNS/proxy/redirect client, bounded startup/request/SSE deadlines,
and starts no replacement or duplicate process.

On a compatible server it verifies unauthenticated health is rejected, then
checks `/global/health` and `/doc` against the exact version/schema pin, creates
a session, attempts one `noReply` no-model/no-tool context message, reads an
actual `/event` SSE event and reconnects with `Last-Event-ID`, aborts the
session, and terminates its one process group before temporary cleanup. Each SSE
read has a five-second deadline, no blocked socket read exceeds three seconds,
and the stream has a 32 KiB total bound; failures report whether cleanup was
confirmed. A message
rejection is reported as `provider_disabled` only for the fixture's exact JSON
`422 {"error":{"code":"provider_disabled","enabled_providers":[]}}`
response. Authentication, route, request-schema, content-type, and every other
response fail rather than being inferred as provider-disabled. It confirms that
the server process group has exited, including ordinary descendants in that
group, before it returns `passed`; failed or uncertain cleanup returns `failed`
with a nonzero CLI exit. This does not detect descendants that deliberately leave
the fixture process group. Synthetic tests cover response classification, lost
SSE responses, explicit reconnect, and process-group cleanup; they are not real
OpenCode evidence. A real `passed` result still proves only this narrow upstream
server compatibility—not enrolled-node trust, provider termination, provider
costs, dispatch, artifact acceptance, or pilot completion.
