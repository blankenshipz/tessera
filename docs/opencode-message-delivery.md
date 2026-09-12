# Context-only OpenCode message delivery

Schema 38 fences the empty, tenant-scoped message-delivery journal to its immutable
operation, grant, and lease tuple. A human can
enqueue one message only after the referenced admitted attempt, revision,
operation epoch, project, worker, harness instance, and upstream session all
match. The stable delivery ID and exact message/context digests are committed
with the queued state before a worker can claim it.

Only the designated worker-audience credential can claim, acknowledge, or
report a delivery. Claim rechecks the active operation, grant, lease, worker,
epoch, attempt revision, project/task/session association and moves `queued`
to `delivered`. Acknowledgement requires the exact context digest,
upstream message identity, and a digest of the upstream readback. Lost replies
are recorded as `uncertain`; they are never retried with another upstream send.
State history is append-only: `queued`, `delivered`, then `acknowledged`,
`uncertain`, `failed`, or a human `cancelled` fence.

The worker uses `LiveOpenCodeHarnessAdapter.deliver_context_only` only when its
explicit context-only mode is enabled. That narrow path verifies the pinned
HTTP schema and a trusted launcher-bound harness instance, sends the source-
inspected OpenCode `v1.18.30`/`3104c1428ec91f809e5ab86631300de41eb6952e`
documented
`{"noReply":true,"parts":[...]}` request only to the designated session, validates
the upstream `{info,parts}` session/message identity and exact intended text while
allowing upstream-generated part IDs/timestamps/metadata,
and keeps a private local intent/readback journal. It has no provider, model
inference, tool, permission, MCP, Kubernetes credential, dispatch, or approval
path. A visible provider, inference, tool, plugin, MCP, or assistant activity
fails provider-disabled mode; lack of a `/provider` request does not prove no
inference. Cancellation is a control-plane fence and does not wait for
OpenCode health or imply upstream session/provider termination.

Existing `POST /interaction/messages` conversations remain legacy messages;
they retain their historical `queued` display but have no delivery/session
record. The desktop labels this distinction and shows uncertain, failed,
cancelled, session, and stale/offline states. This is synthetic local evidence
only: no selected executable/build has been validated against a live OpenCode
server, cluster, or provider. The upstream release/build identity, `/doc` API
schema version/digest, adapter profile, and launcher instance binding are
separate records; source inspection is not live compatibility evidence.
