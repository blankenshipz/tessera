# Context-only OpenCode message delivery

Schema 37 adds an empty, tenant-scoped message-delivery journal. A human can
enqueue one message only after the referenced admitted attempt, revision,
operation epoch, project, worker, harness instance, and upstream session all
match. The stable delivery ID and exact message/context digests are committed
with the queued state before a worker can claim it.

Only the designated worker-audience credential can claim, acknowledge, or
report a delivery. Claim rechecks the active operation/epoch and moves
`queued` to `delivered`. Acknowledgement requires the exact context digest,
upstream message identity, and a digest of the upstream readback. Lost replies
are recorded as `uncertain`; they are never retried with another upstream send.
State history is append-only: `queued`, `delivered`, then `acknowledged`,
`uncertain`, `failed`, or a human `cancelled` fence.

The worker uses `LiveOpenCodeHarnessAdapter.deliver_context_only` only when its
explicit context-only mode is enabled. That narrow path verifies the pinned
HTTP schema and harness instance, sends only the human context to the designated
session, and keeps a private local intent/readback journal. It has no provider,
model inference, tool, permission, MCP, Kubernetes credential, dispatch, or
approval path. Cancellation is a control-plane fence and does not wait for
OpenCode health or imply upstream session/provider termination.

Existing `POST /interaction/messages` conversations remain legacy messages;
they retain their historical `queued` display but have no delivery/session
record. The desktop labels this distinction and shows uncertain, failed,
cancelled, session, and stale/offline states. This is synthetic local evidence
only: no selected executable/build has been validated against a live OpenCode
server, cluster, or provider.
