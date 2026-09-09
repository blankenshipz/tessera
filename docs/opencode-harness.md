# Synthetic OpenCode harness adapter POC

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
