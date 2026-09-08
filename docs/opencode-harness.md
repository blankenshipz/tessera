# Synthetic OpenCode harness adapter POC

`tessera_worker.opencode_harness` is a synthetic, in-process adapter POC. It has
no HTTP implementation, provider call, credential path, control-plane write,
execution dispatch, artifact handling, or result acceptance.

The adapter accepts only an injected five-method client (`version`,
`create_session`, `send_message`, `abort`, and `events`). Before it starts its
deterministic `tessera-opencode-poc-v1` fixture session, it requires an exact
semantic-version pin and a current-authority callback. A false or failing callback
blocks the start; it does not alter grants, budgets, credentials, or policy.

Each accepted event is bounded and carries organization, project, task, operation,
epoch, harness instance, upstream session, cursor, and event identity. Events with
an unknown session or any mismatched binding field are quarantined without changing
the active operation. Event IDs and cursors deduplicate per operation; reconnect
uses the last accepted high-water cursor and never creates another session.

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
